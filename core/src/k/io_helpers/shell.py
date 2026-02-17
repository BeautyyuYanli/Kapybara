"""Async helpers for streaming shell sessions.

This module uses `anyio.open_process` and background tasks to drain
stdout/stderr into a memory stream. The tasks are regular `asyncio` tasks
instead of `anyio.TaskGroup` because `ShellSession` is designed to be held
across multiple tool calls, which may run in different tasks; `anyio.TaskGroup`
scopes must be entered/exited by the same task.

Higher-level callers that need to manage multiple concurrent sessions should
use `ShellSessionManager`, which owns a mapping of session ids to `ShellSession`
instances and provides guardrails for cleanup.
"""

from __future__ import annotations

import asyncio
import secrets
import subprocess
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal, Self

import anyio
from anyio.abc import (
    ByteReceiveStream,
    ByteSendStream,
    Process,
)
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

type NextResult = tuple[bytes, bytes, int | None]
type StreamName = Literal["stdout", "stderr"]


def random_6digits() -> str:
    """Generate a short session id suitable for surfacing to end users.

    Notes:
        This id is intended to be opaque, easy to read/relay, and "unique enough"
        for in-memory session registries. Callers that persist sessions should
        use a stronger id.
    """

    return f"{secrets.randbelow(1_000_000):06d}"


@dataclass(frozen=True, slots=True)
class ShellSessionOptions:
    """Timing/timeout knobs for `ShellSession` (all in seconds)."""

    timeout_seconds: float = 15.0
    idle_output_wait_seconds: float = 0.1
    post_exit_flush_seconds: float = 0.5
    post_exit_drain_wait_seconds: float = 0.01
    terminate_wait_seconds: float = 2.0
    kill_wait_seconds: float = 2.0
    process_close_wait_seconds: float = 0.5


@dataclass(frozen=True)
class _StreamDone:
    stream: StreamName


type OutputEvent = tuple[StreamName, bytes] | _StreamDone


@dataclass(frozen=True, slots=True)
class ShellSessionInfo:
    """Public metadata for an active `ShellSession` registered in a manager.

    Notes:
        `command` is a short, human-readable description derived from
        `command_slug_parts()` (it is not the full raw command).
    """

    session_id: str
    desc: str | None = None


@dataclass(slots=True)
class ShellSessionManager:
    """Manage multiple `ShellSession` instances by `session_id`.

    This is intended for "tool loop" style runtimes where:
    - sessions must be resumable across calls by a short opaque id
    - callers need a single place to prune/interrupt/close sessions

    Concurrency:
        This manager serializes all operations with a single lock. It prioritizes
        correctness and simple invariants over parallelism.
    """

    max_sessions: int = 32

    _sessions: dict[str, ShellSession] = field(default_factory=dict, init=False)
    _lock: anyio.Lock = field(default_factory=anyio.Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("ShellSessionManager is closed")

    async def new_shell(
        self,
        command: str,
        *,
        options: ShellSessionOptions | None = None,
        desc: str | None = None,
    ) -> str:
        """Create, start, and register a new `ShellSession`.

        Returns:
            The new session's `session_id`.
        """

        async with self._lock:
            self._require_open()
            await self._prune_exited_sessions_locked()
            if len(self._sessions) >= self.max_sessions:
                raise RuntimeError(
                    f"Too many active sessions ({len(self._sessions)}/{self.max_sessions})"
                )

            session = ShellSession(
                command, options=options or ShellSessionOptions(), desc=desc
            )
            # Avoid id collisions within this process.
            for _ in range(20):
                if session.session_id not in self._sessions:
                    break
                session.session_id = random_6digits()
            else:
                raise RuntimeError("Failed to allocate a unique session id")

            try:
                await session.ensure_started()
            except BaseException:
                await session.interrupt()
                raise

            self._sessions[session.session_id] = session
            return session.session_id

    async def next(
        self,
        session_id: str,
        stdin: bytes | None = None,
        timeout_seconds: float | None = None,
    ) -> NextResult:
        """Run the next step for a registered session.

        Args:
            session_id: Registered shell session id.
            stdin: Optional stdin bytes to write before waiting.
            timeout_seconds:
                Optional per-call timeout override (seconds) for this `next()`
                call only. If omitted, the session default is used.
        """

        async with self._lock:
            self._require_open()
            session = self._sessions.get(session_id)
            if session is None:
                raise KeyError(f"Unknown session id: {session_id}")

            result = await session.next(stdin=stdin, timeout_seconds=timeout_seconds)
            if session.is_closed():
                self._sessions.pop(session_id, None)
            return result

    async def interrupt(self, session_id: str) -> None:
        """Interrupt and unregister a session (idempotent if already closed)."""

        async with self._lock:
            self._require_open()
            session = self._sessions.pop(session_id, None)
            if session is None:
                raise KeyError(f"Unknown session id: {session_id}")
            await session.interrupt()

    async def clear(self) -> None:
        """Interrupt all managed sessions and clear the registry."""

        async with self._lock:
            self._require_open()
            sessions = list(self._sessions.values())
            self._sessions.clear()

        for session in sessions:
            await session.interrupt()

    async def close(self) -> None:
        """Close the manager and interrupt all managed sessions."""

        async with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()

        for session in sessions:
            await session.interrupt()

    async def list_sessions(self) -> list[ShellSessionInfo]:
        """Prune exited sessions and list remaining active sessions."""

        async with self._lock:
            self._require_open()
            await self._prune_exited_sessions_locked()
            return [
                ShellSessionInfo(
                    session_id=s.session_id,
                    desc=s.desc,
                )
                for s in self._sessions.values()
                if not s.is_closed()
            ]

    async def _prune_exited_sessions_locked(self) -> None:
        """Best-effort prune of sessions whose subprocess has already exited.

        This is mostly a guard for callers that start a session but never call
        `next()`; `ShellSession.next()` normally detects exit and auto-closes.
        """

        to_remove: list[str] = []
        for session_id, session in self._sessions.items():
            if session.is_closed():
                to_remove.append(session_id)
                continue

            process = session._process
            if process is None:
                continue

            if process.returncode is None:
                # Non-blocking poll to refresh returncode if the process has exited.
                with anyio.move_on_after(0):
                    await process.wait()

            if process.returncode is not None:
                await session.interrupt()
                to_remove.append(session_id)

        for session_id in to_remove:
            self._sessions.pop(session_id, None)


@dataclass(slots=True)
class ShellSession:
    """A minimal async session for incremental stdin and drained stdout/stderr.

    API:
    - `__init__(command: str, ...)`
    - `await next(stdin: bytes | None) -> (stdout, stderr, returncode|None)`
    - `await interrupt()`

    `returncode=None` means the subprocess is still running.

    When `next()` observes that the subprocess has exited, it drains any remaining
    stdout/stderr (best-effort) and then automatically calls `interrupt()` to
    release OS resources and mark the session as closed.
    """

    command: str

    options: ShellSessionOptions = field(default_factory=ShellSessionOptions)

    _process: Process | None = field(init=False, default=None, repr=False)
    _stdin: ByteSendStream | None = field(init=False, default=None, repr=False)
    _stdout: ByteReceiveStream | None = field(init=False, default=None, repr=False)
    _stderr: ByteReceiveStream | None = field(init=False, default=None, repr=False)
    _stdout_task: asyncio.Task[None] | None = field(
        init=False, default=None, repr=False
    )
    _stderr_task: asyncio.Task[None] | None = field(
        init=False, default=None, repr=False
    )

    _out_send: MemoryObjectSendStream[OutputEvent] = field(init=False, repr=False)
    _out_recv: MemoryObjectReceiveStream[OutputEvent] = field(init=False, repr=False)

    _closed: bool = field(init=False, default=False, repr=False)
    _stdout_done: bool = field(init=False, default=False, repr=False)
    _stderr_done: bool = field(init=False, default=False, repr=False)

    session_id: str = field(init=False, default_factory=random_6digits)
    desc: str | None = None

    def __post_init__(self) -> None:
        self._out_send, self._out_recv = anyio.create_memory_object_stream[OutputEvent](
            1000
        )

    async def __aenter__(self) -> Self:
        try:
            await self.ensure_started()
            return self
        except BaseException:
            # Best-effort cleanup on enter failures (e.g., spawn errors).
            await self.interrupt()
            raise

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.interrupt()

    async def ensure_started(self) -> None:
        if self._process is not None:
            return
        try:
            thread_name = (
                f"{'-'.join(command_slug_parts(self.command))}-{self.session_id}"
            )
            process = await anyio.open_process(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=False,
            )

            # Publish early so interrupt() can clean up even if we fail mid-start.
            self._process = process

            if (
                process.stdin is None
                or process.stdout is None
                or process.stderr is None
            ):
                raise RuntimeError("Process started without stdin/stdout/stderr pipes")

            # Publish state only after all required resources exist.
            self._stdin = process.stdin
            self._stdout = process.stdout
            self._stderr = process.stderr

            # Use asyncio Tasks instead of anyio.TaskGroup: this session is held across
            # multiple tool calls which may run in different tasks. anyio.TaskGroup
            # requires __aenter__/__aexit__ in the same task, which is easy to violate.
            self._stdout_task = asyncio.create_task(
                self._pump("stdout"),
                name=f"ShellSession({thread_name}):stdout",
            )
            self._stderr_task = asyncio.create_task(
                self._pump("stderr"),
                name=f"ShellSession({thread_name}):stderr",
            )
        except BaseException:
            await self.interrupt()
            raise

    async def _pump(
        self,
        stream_name: StreamName,
    ) -> None:
        stream: ByteReceiveStream | None
        if stream_name == "stdout":
            stream = self._stdout
        else:
            stream = self._stderr
        if stream is None:
            raise RuntimeError(f"Process {stream_name} is not available")

        # Bind to a local var so interrupt() clearing instance fields doesn't affect the reader.
        bound_stream = stream
        try:
            while True:
                try:
                    chunk = await bound_stream.receive(65536)
                except anyio.EndOfStream:
                    break
                if chunk:
                    try:
                        await self._out_send.send((stream_name, chunk))
                    except BaseException:
                        break
        finally:
            # Best-effort signal that this stream is done.
            with suppress(BaseException):
                await self._out_send.send(_StreamDone(stream=stream_name))

    async def next(
        self, stdin: bytes | None = None, timeout_seconds: float | None = None
    ) -> NextResult:
        """Send stdin and wait for output or exit.

        Args:
            stdin: Optional stdin bytes to send first.
            timeout_seconds:
                Optional per-call timeout override in seconds. If omitted, uses
                `self.options.timeout_seconds`.

        Returns:
            (stdout, stderr, returncode). `returncode=None` means the process did
            not exit before the timeout.

        Notes:
            If the process exits (`returncode is not None`), this method performs
            best-effort post-exit output draining and then automatically closes
            the session by calling `interrupt()`. Callers should treat a non-None
            return code as terminal: no further `next()` calls are supported.
        """

        if self._closed:
            raise RuntimeError("Session is closed")
        await self.ensure_started()
        process = self._process
        if process is None:
            raise RuntimeError("Session not started")

        if stdin is not None:
            if self._stdin is None:
                raise RuntimeError("Process stdin is not available")
            await self._stdin.send(stdin)

        stdout = bytearray()
        stderr = bytearray()
        returncode: int | None = None

        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else self.options.timeout_seconds
        )
        if effective_timeout <= 0:
            raise ValueError(f"timeout_seconds must be > 0; got {effective_timeout}")

        deadline = anyio.current_time() + effective_timeout
        while True:
            if process.returncode is not None:
                returncode = process.returncode
                break

            remaining = deadline - anyio.current_time()
            if remaining <= 0:
                returncode = None
                break

            # Drain what we already have; if there was nothing buffered, wait briefly
            # for one event so callers don't need to spin.
            await self._drain_output(stdout, stderr, timeout=0)
            if not stdout and not stderr:
                await self._drain_output(
                    stdout,
                    stderr,
                    timeout=min(self.options.idle_output_wait_seconds, remaining),
                )

            with anyio.move_on_after(0):
                await process.wait()

        # Final drain after exit/timeout. If the process exited, give pumps a brief
        # chance to flush remaining output and send stream-done markers.
        if returncode is not None:
            flush_deadline = anyio.current_time() + self.options.post_exit_flush_seconds
            while anyio.current_time() < flush_deadline and not (
                self._stdout_done and self._stderr_done
            ):
                await self._drain_output(
                    stdout,
                    stderr,
                    timeout=self.options.post_exit_drain_wait_seconds,
                )
        await self._drain_output(stdout, stderr, timeout=0)

        result = (bytes(stdout), bytes(stderr), returncode)

        # If we have a final return code, this session is terminal. Close eagerly
        # so higher-level callers (e.g. tool loops) don't need to remember to call
        # interrupt() to release resources.
        if returncode is not None:
            await self.interrupt()

        return result

    async def _drain_output(
        self, stdout: bytearray, stderr: bytearray, *, timeout: float
    ) -> None:
        """Drain buffered output, then optionally wait up to `timeout` for one event.

        The `timeout` applies only to the single blocking `receive()`; all other
        draining is non-blocking.
        """

        # Drain whatever is already buffered (non-blocking).
        while True:
            try:
                event = self._out_recv.receive_nowait()
            except anyio.WouldBlock, anyio.EndOfStream:
                break
            self._apply_event(event, stdout, stderr)

        if timeout <= 0:
            return

        # Wait up to `timeout` for a single output event, then drain any burst.
        cancel_exc = anyio.get_cancelled_exc_class()
        event = None
        with anyio.move_on_after(timeout):
            try:
                event = await self._out_recv.receive()
            except anyio.EndOfStream:
                return
            except cancel_exc:
                event = None
        if event is not None:
            self._apply_event(event, stdout, stderr)

        while True:
            try:
                event = self._out_recv.receive_nowait()
            except anyio.WouldBlock, anyio.EndOfStream:
                break
            self._apply_event(event, stdout, stderr)

    def _apply_event(
        self, event: OutputEvent, stdout: bytearray, stderr: bytearray
    ) -> None:
        if isinstance(event, _StreamDone):
            if event.stream == "stdout":
                self._stdout_done = True
            else:
                self._stderr_done = True
            return

        stream_name, chunk = event
        if stream_name == "stdout":
            stdout += chunk
        else:
            stderr += chunk

    async def interrupt(self) -> None:
        """Best-effort cleanup hook (never raises).

        This is intentionally tolerant of partial initialization and cleanup
        failures; callers should not need to wrap it in try/except.
        """

        if self._closed:
            return
        self._closed = True

        try:
            with anyio.CancelScope(shield=True):
                process = self._process
                stdout_task = self._stdout_task
                stderr_task = self._stderr_task

                # Terminate/kill first to encourage pumps to finish naturally.
                if process is not None and process.returncode is None:
                    with suppress(BaseException):
                        process.terminate()
                    with anyio.move_on_after(self.options.terminate_wait_seconds):
                        with suppress(BaseException):
                            await process.wait()
                    if process.returncode is None:
                        with suppress(BaseException):
                            process.kill()
                        with anyio.move_on_after(self.options.kill_wait_seconds):
                            with suppress(BaseException):
                                await process.wait()

                # Cancel reader tasks before closing the output stream to avoid
                # concurrent sends into a closed channel.
                for task in (stdout_task, stderr_task):
                    if task is None:
                        continue
                    with suppress(BaseException):
                        task.cancel()
                for task in (stdout_task, stderr_task):
                    if task is None:
                        continue
                    with suppress(BaseException):
                        await task

                with suppress(BaseException):
                    await self._out_send.aclose()

                if process is not None:
                    with anyio.move_on_after(self.options.process_close_wait_seconds):
                        with suppress(BaseException):
                            await process.aclose()
        except BaseException:
            # Cleanup must never leak exceptions (including cancellation) out to callers.
            pass
        finally:
            self._process = None
            self._stdin = None
            self._stdout = None
            self._stderr = None
            self._stdout_task = None
            self._stderr_task = None

    def is_closed(self) -> bool:
        """Check if the session has been closed."""
        return self._closed

    def __del__(self) -> None:
        # Best-effort sync cleanup path for abnormal object destruction.
        try:
            process = getattr(self, "_process", None)
            if process is not None and getattr(process, "returncode", None) is None:
                process.terminate()
        except Exception:
            pass


def command_slug_parts(
    command: str,
    *,
    head_components: int = 3,
    tail_components: int = 2,
    component_limit: int = 24,
    label_limit: int = 80,
) -> list[str]:
    """Generate a short, human-readable slug preview for a shell command.

    Returns:
        A list of slug components suitable for joining with `-` into a task name.

    Notes:
        This intentionally uses only a preview of the command (first few + last few
        whitespace-delimited components) so we don't embed full command lines (which
        can be long and/or sensitive) into asyncio task names that may surface in
        logs and debuggers.
    """

    normalized = " ".join(command.strip().split())
    if not normalized:
        return ["cmd"]

    components = normalized.split(" ")

    def clean(component: str) -> str:
        component = component.strip()
        if (
            len(component) >= 2
            and component[0] == component[-1]
            and component[0] in {"'", '"'}
        ):
            component = component[1:-1].strip()
        component = component.rsplit("/", 1)[-1]
        if len(component) > component_limit:
            component = component[: max(0, component_limit - 1)] + "…"
        return component or "…"

    head = [clean(c) for c in components[:head_components]]
    tail_start = max(head_components, len(components) - tail_components)
    tail = [clean(c) for c in components[tail_start:]]

    preview: list[str] = [*head]
    if tail_start > head_components and tail:
        preview.append("…")
    preview.extend(tail)

    # Ensure the final joined label stays reasonably short.
    label = "-".join(preview).strip("-")
    if len(label) > label_limit and preview:
        prefix = "-".join(preview[:-1]).strip("-")
        remaining = label_limit - (len(prefix) + (1 if prefix else 0))
        if remaining <= 0:
            return ["cmd"]
        last = preview[-1]
        if len(last) > remaining:
            preview[-1] = last[: max(0, remaining - 1)] + "…"

    return [p for p in preview if p] or ["cmd"]

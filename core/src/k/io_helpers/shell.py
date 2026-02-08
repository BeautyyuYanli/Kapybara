"""Async helpers for streaming shell sessions.

This module intentionally uses `anyio.open_process` and task groups instead of
manual reader threads for stdout/stderr. The core API is a small stateful
session with incremental stdin and non-blocking output drains.
"""

from __future__ import annotations

import subprocess
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Literal, Self, TypeAlias

import anyio
from anyio.abc import (
    ByteReceiveStream,
    ByteSendStream,
    Process,
    TaskGroup,
)
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream


type NextResult = tuple[bytes, bytes, int | None]
StreamName: TypeAlias = Literal["stdout", "stderr"]


@dataclass(frozen=True)
class _StreamDone:
    stream: StreamName


OutputEvent: TypeAlias = tuple[StreamName, bytes] | _StreamDone


@dataclass
class ShellSession:
    """A minimal async session for incremental stdin and drained stdout/stderr.

    API:
    - `__init__(command: str, ...)`
    - `await next(stdin: bytes | None) -> (stdout, stderr, returncode|None)`
    - `await interrupt()`

    `returncode=None` means the subprocess is still running.
    """

    command: str

    # Timeout/timing knobs (all in seconds).
    timeout_seconds: float = 5.0
    idle_output_wait_seconds: float = 0.1
    post_exit_flush_seconds: float = 0.5
    post_exit_drain_wait_seconds: float = 0.01
    terminate_wait_seconds: float = 2.0
    kill_wait_seconds: float = 2.0
    process_close_wait_seconds: float = 0.5

    _process: Process | None = field(init=False, default=None, repr=False)
    _stdin: ByteSendStream | None = field(init=False, default=None, repr=False)
    _stdout: ByteReceiveStream | None = field(init=False, default=None, repr=False)
    _stderr: ByteReceiveStream | None = field(init=False, default=None, repr=False)
    _tg: TaskGroup | None = field(init=False, default=None, repr=False)

    _out_send: MemoryObjectSendStream[OutputEvent] = field(init=False, repr=False)
    _out_recv: MemoryObjectReceiveStream[OutputEvent] = field(init=False, repr=False)

    _closed: bool = field(init=False, default=False, repr=False)
    _stdout_done: bool = field(init=False, default=False, repr=False)
    _stderr_done: bool = field(init=False, default=False, repr=False)

    def __post_init__(self) -> None:
        self._out_send, self._out_recv = anyio.create_memory_object_stream[OutputEvent](1000)

    async def __aenter__(self) -> Self:
        try:
            await self._ensure_started()
            return self
        except BaseException:
            # Best-effort cleanup on enter failures (e.g., spawn errors).
            await self.interrupt()
            raise

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.interrupt()

    async def _ensure_started(self) -> None:
        if self._process is not None:
            return
        try:
            process = await anyio.open_process(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=False,
            )

            # Publish early so interrupt() can clean up even if we fail mid-start.
            self._process = process

            if process.stdin is None or process.stdout is None or process.stderr is None:
                raise RuntimeError("Process started without stdin/stdout/stderr pipes")

            tg = await anyio.create_task_group().__aenter__()

            # Publish state only after all required resources exist.
            self._stdin = process.stdin
            self._stdout = process.stdout
            self._stderr = process.stderr
            self._tg = tg

            tg.start_soon(self._pump, "stdout")
            tg.start_soon(self._pump, "stderr")
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
            try:
                await self._out_send.send(_StreamDone(stream=stream_name))
            except Exception:
                pass

    async def next(self, stdin: bytes | None) -> NextResult:
        """Send stdin and wait up to `timeout_seconds` for process exit.

        Returns:
            (stdout, stderr, returncode). `returncode=None` means the process did
            not exit before the timeout.
        """

        if self._closed:
            raise RuntimeError("Session is closed")
        await self._ensure_started()
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

        deadline = anyio.current_time() + self.timeout_seconds
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
                    timeout=min(self.idle_output_wait_seconds, remaining),
                )

            with anyio.move_on_after(0):
                await process.wait()

        # Final drain after exit/timeout. If the process exited, give pumps a brief
        # chance to flush remaining output and send stream-done markers.
        if returncode is not None:
            flush_deadline = anyio.current_time() + self.post_exit_flush_seconds
            while anyio.current_time() < flush_deadline and not (self._stdout_done and self._stderr_done):
                await self._drain_output(stdout, stderr, timeout=self.post_exit_drain_wait_seconds)
        await self._drain_output(stdout, stderr, timeout=0)

        return (bytes(stdout), bytes(stderr), returncode)

    async def _drain_output(self, stdout: bytearray, stderr: bytearray, *, timeout: float) -> None:
        """Drain buffered output, then optionally wait up to `timeout` for one event.

        The `timeout` applies only to the single blocking `receive()`; all other
        draining is non-blocking.
        """

        # Drain whatever is already buffered (non-blocking).
        while True:
            try:
                event = self._out_recv.receive_nowait()
            except (anyio.WouldBlock, anyio.EndOfStream):
                break
            self._apply_event(event, stdout, stderr)

        if timeout <= 0:
            return

        # Wait up to `timeout` for a single output event, then drain any burst.
        cancel_exc = anyio.get_cancelled_exc_class()
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
            except (anyio.WouldBlock, anyio.EndOfStream):
                break
            self._apply_event(event, stdout, stderr)

    def _apply_event(self, event: OutputEvent, stdout: bytearray, stderr: bytearray) -> None:
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
                tg = self._tg

                # Terminate/kill first to encourage pumps to finish naturally.
                if process is not None and process.returncode is None:
                    with suppress(BaseException):
                        process.terminate()
                    with anyio.move_on_after(self.terminate_wait_seconds):
                        with suppress(BaseException):
                            await process.wait()
                    if process.returncode is None:
                        with suppress(BaseException):
                            process.kill()
                        with anyio.move_on_after(self.kill_wait_seconds):
                            with suppress(BaseException):
                                await process.wait()

                # Cancel reader tasks before closing the output stream to avoid
                # concurrent sends into a closed channel.
                if tg is not None:
                    with suppress(BaseException):
                        await tg.__aexit__(None, None, None)

                with suppress(BaseException):
                    await self._out_send.aclose()

                if process is not None:
                    with anyio.move_on_after(self.process_close_wait_seconds):
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
            self._tg = None

    def __del__(self) -> None:
        # Best-effort sync cleanup path for abnormal object destruction.
        try:
            process = getattr(self, "_process", None)
            if process is not None and getattr(process, "returncode", None) is None:
                process.terminate()
        except Exception:
            pass

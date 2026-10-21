"""Native Rust I/O on the caller's asyncio loop, with shared OpenSOL DTOs.

Constructors and conversions only bind local clients and require no running loop.
I/O methods are coroutines: native Futures are created only when they run.
Each AsyncOutputSubscription owns one native stream. Read operations are serialized;
cancellation retains a reserved event until a Python consumer acknowledges it.
Closing a subscription never signals the process or deletes its environment.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import TYPE_CHECKING, Literal

from . import _native
from .config import BackendConfig, SandboxSpec, _RuntimeBackend, _RuntimeConnection
from .config import _connection, _invalid, _source, _spec, _target

if TYPE_CHECKING:
    from ._sync import Backend, Connection, ProcessManager, SandboxManager
from ._native import (
    ExecResult,
    ManagerInfo,
    OutputCursor,
    OutputPage,
    TerminalCursor,
    TerminalPage,
    TerminalScreen,
    ProcessEvent,
    ProcessInfo,
    ProcessOutcome,
    ProcessRef,
    SandboxInfo,
    SandboxRef,
)


class AsyncBackend(_RuntimeBackend):
    """Native access context, with no ownership of remote resource lifetimes."""

    def __init__(self, config: BackendConfig, *, connection: AsyncConnection | Connection | None = None) -> None:
        value = _source(config)
        if not isinstance(value, str):
            raise _invalid("AsyncBackend requires a BackendConfig")
        self._native = _native._new_backend(value, _connection(connection))

    def to_backend(self) -> Backend:
        """Return a blocking view of this context, without I/O."""
        from ._sync import Backend
        return Backend._from_async(self)


class AsyncConnection(_RuntimeConnection):
    """Completed commands on one target, scheduled when their coroutine runs."""

    def __init__(self, source: BackendConfig | AsyncBackend | Backend, *, target: SandboxRef | None = None) -> None:
        self._native = _native._new_connection(_source(source), _target(target))

    def to_connection(self) -> Connection:
        """Return a blocking view of the same client and target, without I/O."""
        from ._sync import Connection
        return Connection._from_async(self)

    @property
    def backend(self) -> str:
        return self._native.backend

    def capabilities(self) -> _native.ConnectionCapabilities:
        return self._native.capabilities()

    async def exec(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes = b"",
        timeout: float | None = None,
    ) -> ExecResult:
        return await self._native.exec(argv, cwd=cwd, env=env, stdin=stdin, timeout=timeout)

    async def shell(
        self,
        script: str,
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bytes = b"",
        timeout: float | None = None,
    ) -> ExecResult:
        return await self._native.shell(script, cwd=cwd, env=env, stdin=stdin, timeout=timeout)


class AsyncSandboxManager:
    """Create, discover and locally bind sandboxes within one backend scope."""

    def __init__(self, source: BackendConfig | AsyncBackend | Backend) -> None:
        self._native = _native._new_sandboxes(_source(source))

    def to_sandbox_manager(self) -> SandboxManager:
        """Return a blocking view of this manager, without I/O."""
        from ._sync import SandboxManager
        return SandboxManager._from_async(self)

    @property
    def backend(self) -> str:
        return self._native.backend

    def capabilities(self) -> _native.SandboxManagerCapabilities:
        return self._native.capabilities()

    def get(self, sandbox: SandboxRef) -> AsyncSandbox:
        """Bind locally; existence and permissions are checked on later I/O."""
        return AsyncSandbox(self._native.get(sandbox))

    async def list(self) -> list[SandboxInfo]:
        return await self._native.list()

    async def create(self, spec: SandboxSpec) -> AsyncSandbox:
        return AsyncSandbox(await self._native.create(_spec(spec)))


class AsyncSandbox:
    """Native requests without a common readiness or state-convergence barrier.

    Observe state with inspect; dropping this object never deletes the environment.
    """

    def __init__(self, native: _native.AsyncSandbox) -> None:
        self._native = native

    @property
    def ref(self) -> SandboxRef:
        return self._native.ref

    def capabilities(self) -> _native.SandboxCapabilities:
        return self._native.capabilities()

    async def inspect(self) -> SandboxInfo:
        return await self._native.inspect()

    async def start(self) -> None:
        await self._native.start()

    async def stop(self) -> None:
        await self._native.stop()

    async def delete(self) -> None:
        await self._native.delete()

    async def fork(self) -> AsyncSandbox:
        return AsyncSandbox(await self._native.fork())


class AsyncOutputSubscription(AsyncIterator[ProcessEvent]):
    """One subscription; completion arrives as an event, ``aclose`` only detaches.

    The asyncio lock is owned by the Python coroutine, so it is released on
    cancellation. Rust keeps its single reserved event until the resumed Python
    coroutine confirms delivery without another await between those steps.
    """

    def __init__(self, native: _native.AsyncOutputSubscription) -> None:
        self._native = native
        self._reader = asyncio.Lock()

    def capabilities(self) -> _native.SubscriptionDelivery:
        """Return the actual native source contract, unchanged by reads or closure."""
        return self._native.capabilities()

    async def next_event(self, *, timeout: float | None = None) -> ProcessEvent | None:
        """Read one event; timeout/cancellation leaves the subscription usable.

        The timeout covers waiting for the next native event after this reader
        acquires its turn. It does not limit time queued behind another reader.
        """
        async with self._reader:
            delivery = self._native._reserve(timeout=timeout)
            try:
                event = await delivery
            except asyncio.CancelledError:
                raise
            except Exception:
                # A terminal stream error also stays reserved until Python has
                # received it. Parameter errors before creating the awaitable do
                # not acknowledge any pending event.
                self._native._commit()
                raise
            self._native._commit()
            return event

    async def aclose(self) -> None:
        """Detach and wake blocked readers; safe to call repeatedly."""
        await self._native.aclose()

    def __aiter__(self) -> AsyncOutputSubscription:
        return self

    async def __anext__(self) -> ProcessEvent:
        event = await self.next_event()
        if event is None:
            raise StopAsyncIteration
        return event


class AsyncProcessManager:
    """Find and spawn native tasks; use ``get`` to bind a Process for task operations."""

    def __init__(self, source: BackendConfig | AsyncBackend | Backend, *, target: SandboxRef | None = None,
                 connection: AsyncConnection | Connection | None = None) -> None:
        self._native = _native._new_processes(_source(source), _target(target), _connection(connection))

    def to_process_manager(self) -> ProcessManager:
        """Return a blocking view of this manager, without I/O."""
        from ._sync import ProcessManager
        return ProcessManager._from_async(self)

    @property
    def backend(self) -> str:
        return self._native.backend

    def capabilities(self) -> _native.ProcessManagerCapabilities:
        return self._native.capabilities()

    def get(self, process: ProcessRef) -> AsyncProcess:
        """Bind a resource locally; existence is checked on its next native operation."""
        return AsyncProcess(self._native.get(process))

    async def probe(self) -> ManagerInfo:
        return await self._native.probe()

    async def list(self) -> list[ProcessInfo]:
        return await self._native.list()

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bool = False,
        pty: tuple[int, int] | None = None,
    ) -> AsyncProcess:
        """Create a task, preserving an indispensable Start stream only on envd."""
        return AsyncProcess(
            await self._native.spawn(argv, cwd=cwd, env=env, stdin=stdin, pty=pty)
        )


class AsyncProcess:
    """Bound task operations, logs and screen; no cached status or lifecycle ownership."""

    def __init__(self, native: _native.AsyncProcess) -> None:
        self._native = native

    @property
    def ref(self) -> ProcessRef:
        return self._native.ref

    def capabilities(self) -> _native.ProcessCapabilities:
        """Local mode-aware control, without probing existence, permissions or state."""
        return self._native.capabilities()

    async def inspect(self) -> ProcessInfo:
        return await self._native.inspect()

    async def wait(self, *, timeout: float | None = None) -> ProcessOutcome:
        return await self._native.wait(timeout=timeout)

    async def pause(self) -> None:
        await self._native.pause()

    async def start(self) -> None:
        await self._native.start()

    async def restart(self) -> None:
        await self._native.restart()

    async def kill(self) -> None:
        await self._native.kill()

    async def remove(self) -> None:
        await self._native.remove()

    async def signal(self, signal: int) -> None:
        await self._native.signal(signal)

    async def send_input(self, data: bytes) -> None:
        await self._native.send_input(data)

    async def close_stdin(self) -> None:
        await self._native.close_stdin()

    async def resize(self, cols: int, rows: int) -> None:
        await self._native.resize(cols, rows)

    @property
    def output(self) -> AsyncOutput:
        return AsyncOutput(self._native.output)

    @property
    def terminal(self) -> AsyncTerminal:
        return AsyncTerminal(self._native.terminal)


class AsyncOutput:
    """Locally bound raw bytes; page cursors and subscriptions consume independently."""

    def __init__(self, native: _native.AsyncOutput) -> None:
        self._native = native

    def capabilities(self) -> _native.OutputCapabilities:
        return self._native.capabilities()

    async def read(self, *, cursor: OutputCursor | None = None,
                   direction: Literal["forward", "backward"] = "forward", max_bytes: int = 65536) -> OutputPage:
        return await self._native.read(cursor=cursor, direction=direction, max_bytes=max_bytes)

    async def tail(self, *, max_bytes: int = 65536) -> OutputPage:
        return await self._native.tail(max_bytes=max_bytes)

    async def subscribe(self) -> AsyncOutputSubscription:
        return AsyncOutputSubscription(await self._native.subscribe())


class AsyncTerminal:
    """Native rendered text, read without interpreting bytes or owning a process."""

    def __init__(self, native: _native.AsyncTerminal) -> None:
        self._native = native

    def capabilities(self) -> _native.TerminalCapabilities:
        return self._native.capabilities()

    async def read(self, *, cursor: TerminalCursor | None = None,
                   direction: Literal["forward", "backward"] = "forward", lines: int = 200) -> TerminalPage:
        return await self._native.read(cursor=cursor, direction=direction, lines=lines)

    async def tail(self, *, lines: int = 200) -> TerminalPage:
        return await self._native.tail(lines=lines)

    async def screen(self) -> TerminalScreen:
        return await self._native.screen()


__all__ = [
    "AsyncBackend", "AsyncConnection", "AsyncSandboxManager", "AsyncSandbox", "AsyncProcessManager",
    "AsyncOutputSubscription", "AsyncProcess", "AsyncOutput", "AsyncTerminal",
]

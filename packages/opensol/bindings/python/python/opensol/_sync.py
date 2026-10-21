"""Blocking views of the async clients. Native Rust I/O releases the GIL.

Each wrapper retains its async view; conversion shares the context and performs
no I/O. Blocking calls drive the same Rust core without running a Python loop.
"""

from __future__ import annotations

from collections.abc import Sequence

from . import _native
from ._async import AsyncBackend, AsyncConnection, AsyncProcessManager, AsyncSandboxManager
from .config import BackendConfig, SandboxSpec, _RuntimeBackend, _RuntimeConnection
from .config import _spec, _target


class Backend(_RuntimeBackend):
    """Opened access context. Releasing it never deletes sandboxes or kills processes."""

    def __init__(self, config: BackendConfig, *, connection: AsyncConnection | Connection | None = None) -> None:
        self._async = AsyncBackend(config, connection=connection)
        self._native = self._async._native

    @classmethod
    def _from_async(cls, view: AsyncBackend) -> Backend:
        result = cls.__new__(cls)
        result._async = view
        result._native = view._native
        return result

    def to_async_backend(self) -> AsyncBackend:
        return self._async


class Connection(_RuntimeConnection):
    """Completed commands on a fixed target; no physical long-lived connection is promised."""

    def __init__(self, source: BackendConfig | AsyncBackend | Backend, *, target: _native.SandboxRef | None = None) -> None:
        self._async = AsyncConnection(source, target=target)
        self._native = self._async._native._blocking()

    @classmethod
    def _from_async(cls, view: AsyncConnection) -> Connection:
        result = cls.__new__(cls)
        result._async = view
        result._native = view._native._blocking()
        return result

    def to_async_connection(self) -> AsyncConnection:
        return self._async

    @property
    def backend(self) -> str:
        return self._native.backend

    def capabilities(self) -> _native.ConnectionCapabilities:
        return self._native.capabilities()

    def exec(self, argv: Sequence[str], *, cwd: str | None = None,
             env: dict[str, str] | None = None, stdin: bytes = b"",
             timeout: float | None = None) -> _native.ExecResult:
        return self._native.exec(argv, cwd=cwd, env=env, stdin=stdin, timeout=timeout)

    def shell(self, script: str, *, cwd: str | None = None,
              env: dict[str, str] | None = None, stdin: bytes = b"",
              timeout: float | None = None) -> _native.ExecResult:
        return self._native.shell(script, cwd=cwd, env=env, stdin=stdin, timeout=timeout)


class SandboxManager:
    """Lifecycle API scoped to one Backend. Each create receives a complete native spec."""

    def __init__(self, source: BackendConfig | AsyncBackend | Backend) -> None:
        self._async = AsyncSandboxManager(source)
        self._native = self._async._native._blocking()

    @classmethod
    def _from_async(cls, view: AsyncSandboxManager) -> SandboxManager:
        result = cls.__new__(cls)
        result._async = view
        result._native = view._native._blocking()
        return result

    def to_async_sandbox_manager(self) -> AsyncSandboxManager:
        return self._async

    @property
    def backend(self) -> str:
        return self._native.backend

    def capabilities(self) -> _native.SandboxManagerCapabilities:
        return self._native.capabilities()

    def get(self, sandbox: _native.SandboxRef) -> _native.Sandbox:
        return self._native.get(_target(sandbox))

    def list(self) -> list[_native.SandboxInfo]:
        return self._native.list()

    def create(self, spec: SandboxSpec) -> _native.Sandbox:
        return self._native.create(_spec(spec))


class ProcessManager:
    """Native task API; Pueue/Tmux depend on an explicitly injected Connection."""

    def __init__(self, source: BackendConfig | AsyncBackend | Backend, *, target: _native.SandboxRef | None = None,
                 connection: AsyncConnection | Connection | None = None) -> None:
        self._async = AsyncProcessManager(source, target=target, connection=connection)
        self._native = self._async._native._blocking()

    @classmethod
    def _from_async(cls, view: AsyncProcessManager) -> ProcessManager:
        result = cls.__new__(cls)
        result._async = view
        result._native = view._native._blocking()
        return result

    def to_async_process_manager(self) -> AsyncProcessManager:
        return self._async

    @property
    def backend(self) -> str:
        return self._native.backend

    def capabilities(self) -> _native.ProcessManagerCapabilities:
        return self._native.capabilities()

    def get(self, process: _native.ProcessRef) -> _native.Process:
        return self._native.get(process)

    def probe(self) -> _native.ManagerInfo:
        return self._native.probe()

    def list(self) -> list[_native.ProcessInfo]:
        return self._native.list()

    def spawn(self, argv: Sequence[str], *, cwd: str | None = None,
              env: dict[str, str] | None = None, stdin: bool = False,
              pty: tuple[int, int] | None = None) -> _native.Process:
        return self._native.spawn(argv, cwd=cwd, env=env, stdin=stdin, pty=pty)

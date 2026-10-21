"""Blocking public clients. Native I/O releases the GIL and shares the Rust construction path."""

from __future__ import annotations

from collections.abc import Sequence

from . import _native
from .config import BackendConfig, SandboxSpec, _RuntimeBackend, _RuntimeConnection
from .config import _connection, _invalid, _source, _spec, _target


class Backend(_RuntimeBackend):
    """Opened access context. Releasing it never deletes sandboxes or kills processes."""

    @staticmethod
    def open(config: BackendConfig, *, connection: Connection | None = None) -> Backend:
        value = _source(config)
        if not isinstance(value, str):
            raise _invalid("Backend.open requires a BackendConfig")
        return Backend(_native._open_backend(value, _connection(connection)))


class Connection(_RuntimeConnection):
    """Completed commands on a fixed target; no physical long-lived connection is promised."""

    @staticmethod
    def open(source: BackendConfig | Backend, *, target: _native.SandboxRef | None = None) -> Connection:
        return Connection(_native._open_connection(_source(source), _target(target)))

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

    def __init__(self, native: _native.SandboxManager) -> None:
        self._native = native

    @staticmethod
    def open(source: BackendConfig | Backend) -> SandboxManager:
        return SandboxManager(_native._open_sandboxes(_source(source)))

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

    def __init__(self, native: _native.ProcessManager) -> None:
        self._native = native

    @staticmethod
    def open(source: BackendConfig | Backend, *, target: _native.SandboxRef | None = None,
             connection: Connection | None = None) -> ProcessManager:
        return ProcessManager(_native._open_processes(_source(source), _target(target), _connection(connection)))

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

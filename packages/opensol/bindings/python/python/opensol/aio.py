"""Native Rust I/O on the caller's asyncio loop, with shared OpenSOL DTOs.

Public I/O methods and network factories are coroutines: native Futures are
created only when the coroutine runs. Configuration and local binding stay synchronous.
Each OutputSubscription owns one native stream. Read operations are serialized;
cancellation retains a reserved event until a Python consumer acknowledges it.
Closing a subscription never signals the process or deletes its environment.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from typing import Literal

from . import _native
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


class Connection:
    """Completed commands on one target, scheduled when their coroutine runs."""

    def __init__(self, native: _native.AsyncConnection) -> None:
        self._native = native

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


class SandboxManager:
    """Create, discover and locally bind sandboxes within one backend scope."""

    def __init__(self, native: _native.AsyncSandboxManager) -> None:
        self._native = native

    @property
    def backend(self) -> str:
        return self._native.backend

    def capabilities(self) -> _native.SandboxManagerCapabilities:
        return self._native.capabilities()

    def get(self, sandbox: SandboxRef) -> Sandbox:
        """Bind locally; existence and permissions are checked on later I/O."""
        return Sandbox(self._native.get(sandbox))

    async def list(self) -> list[SandboxInfo]:
        return await self._native.list()

    async def create(self, *, name: str | None = None, labels: dict[str, str] | None = None) -> Sandbox:
        return Sandbox(await self._native.create(name=name, labels=labels))


class Sandbox:
    """Single sandbox operations; dropping this object never deletes the environment."""

    def __init__(self, native: _native.AsyncSandbox) -> None:
        self._native = native

    @property
    def ref(self) -> SandboxRef:
        return self._native.ref

    async def inspect(self) -> SandboxInfo:
        return await self._native.inspect()

    async def start(self) -> None:
        await self._native.start()

    async def stop(self) -> None:
        await self._native.stop()

    async def delete(self) -> None:
        await self._native.delete()

    async def fork(self) -> Sandbox:
        return Sandbox(await self._native.fork())


class OutputSubscription(AsyncIterator[ProcessEvent]):
    """One subscription; completion arrives as an event, ``aclose`` only detaches.

    The asyncio lock is owned by the Python coroutine, so it is released on
    cancellation. Rust keeps its single reserved event until the resumed Python
    coroutine confirms delivery without another await between those steps.
    """

    def __init__(self, native: _native.AsyncOutputSubscription) -> None:
        self._native = native
        self._reader = asyncio.Lock()

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

    def __aiter__(self) -> OutputSubscription:
        return self

    async def __anext__(self) -> ProcessEvent:
        event = await self.next_event()
        if event is None:
            raise StopAsyncIteration
        return event


class ProcessManager:
    """Find and spawn native tasks; use ``get`` to bind a Process for task operations."""

    def __init__(self, native: _native.AsyncProcessManager) -> None:
        self._native = native

    @property
    def backend(self) -> str:
        return self._native.backend

    def capabilities(self) -> _native.ProcessManagerCapabilities:
        return self._native.capabilities()

    def get(self, process: ProcessRef) -> Process:
        """Bind a resource locally; existence is checked on its next native operation."""
        return Process(self._native.get(process))

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
    ) -> Process:
        """Create a task; its first output subscription takes the native initial stream."""
        return Process(
            await self._native.spawn(argv, cwd=cwd, env=env, stdin=stdin, pty=pty)
        )


class Process:
    """Bound task operations, logs and screen; no cached status or lifecycle ownership."""

    def __init__(self, native: _native.AsyncProcess) -> None:
        self._native = native

    @property
    def ref(self) -> ProcessRef:
        return self._native.ref

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
    def output(self) -> Output:
        return Output(self._native.output)

    @property
    def terminal(self) -> Terminal:
        return Terminal(self._native.terminal)


class Output:
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

    async def subscribe(self) -> OutputSubscription:
        return OutputSubscription(await self._native.subscribe())


class Terminal:
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


class Envd:
    """One envd client exposing independent async connection and process APIs."""

    def __init__(self, native: _native.Envd) -> None:
        self._native = native

    @property
    def connection(self) -> Connection:
        return Connection(_native._async_connection(self._native.connection))

    @property
    def processes(self) -> ProcessManager:
        return ProcessManager(_native._async_processes(self._native.processes))


class Docker:
    """Configured Engine context; lifecycle and connection binding remain explicit."""

    def __init__(self, native: _native.Docker) -> None:
        self._native = native

    @property
    def sandboxes(self) -> SandboxManager:
        return SandboxManager(_native._async_sandboxes(self._native.sandboxes))

    def connection(self, resource: SandboxRef) -> Connection:
        return Connection(_native._async_connection(self._native.connection(resource)))


class E2b:
    """One control-plane context, including credentials used to discover envd."""

    def __init__(self, native: _native.E2b) -> None:
        self._native = native

    @property
    def sandboxes(self) -> SandboxManager:
        return SandboxManager(_native._async_sandboxes(self._native.sandboxes))

    async def envd(self, resource: SandboxRef) -> Envd:
        return Envd(await self._native._aio_envd(resource))


class OpenShell:
    """Established gateway and workspace context; connection binding is local."""

    def __init__(self, native: _native.OpenShell) -> None:
        self._native = native

    @property
    def sandboxes(self) -> SandboxManager:
        return SandboxManager(_native._async_sandboxes(self._native.sandboxes))

    def connection(self, resource: SandboxRef) -> Connection:
        return Connection(_native._async_connection(self._native.connection(resource)))


def local() -> Connection:
    return Connection(_native._async_connection(_native.local()))


def ssh(
    destination: str,
    *,
    port: int | None = None,
    identity_file: str | None = None,
    config_file: str | None = None,
    program: str = "ssh",
) -> Connection:
    return Connection(
        _native._async_connection(
            _native.ssh(destination, port=port, identity_file=identity_file,
                        config_file=config_file, program=program)
        )
    )


def docker(
    image: str,
    *,
    command: Sequence[str] | None = None,
    env: dict[str, str] | None = None,
    working_dir: str | None = None,
    host_config_json: str | None = None,
) -> Docker:
    return Docker(
        _native.docker(image, command=command, env=env, working_dir=working_dir,
                       host_config_json=host_config_json)
    )


def docker_connection(container: str) -> Connection:
    return Connection(_native._async_connection(_native.docker_connection(container)))


def pueue(
    connection: Connection,
    *,
    config_file: str | None = None,
    profile: str | None = None,
    group: str | None = None,
    pueue_directory: str | None = None,
    program: str = "pueue",
) -> ProcessManager:
    manager = _native.pueue(connection._native._blocking(), config_file=config_file,
                            profile=profile, group=group, pueue_directory=pueue_directory,
                            program=program)
    return ProcessManager(_native._async_processes(manager))


def tmux(
    connection: Connection,
    *,
    socket_path: str,
    session_prefix: str = "opensol",
    program: str = "tmux",
) -> ProcessManager:
    """Bind a caller-owned tmux server on the Connection target without I/O."""
    manager = _native.tmux(connection._native._blocking(), socket_path=socket_path,
                           session_prefix=session_prefix, program=program)
    return ProcessManager(_native._async_processes(manager))


def envd(
    endpoint: str,
    *,
    access_token: str | None = None,
    user: str | None = None,
    sandbox_id: str | None = None,
    request_timeout: float = 30.0,
) -> Envd:
    return Envd(_native.envd(endpoint, access_token=access_token, user=user,
                             sandbox_id=sandbox_id, request_timeout=request_timeout))


def e2b(
    api_key: str,
    template: str = "base",
    *,
    api_endpoint: str = "https://api.e2b.app",
    sandbox_timeout: int = 300,
    auto_pause: bool = False,
    env: dict[str, str] | None = None,
    metadata: dict[str, str] | None = None,
    envd_endpoint: str | None = None,
    sandbox_domain: str = "e2b.app",
    request_timeout: float = 30.0,
) -> E2b:
    return E2b(
        _native.e2b(api_key, template, api_endpoint=api_endpoint,
                    sandbox_timeout=sandbox_timeout, auto_pause=auto_pause,
                    env=env, metadata=metadata, envd_endpoint=envd_endpoint,
                    sandbox_domain=sandbox_domain, request_timeout=request_timeout)
    )


def agent_sandbox(
    namespace: str,
    pod_template_json: str,
    *,
    context: str | None = None,
    kubeconfig: str | None = None,
    program: str = "kubectl",
    service: bool = True,
    volume_claim_templates_json: str | None = None,
) -> SandboxManager:
    return SandboxManager(
        _native._async_sandboxes(
            _native.agent_sandbox(
                namespace, pod_template_json, context=context, kubeconfig=kubeconfig,
                program=program, service=service,
                volume_claim_templates_json=volume_claim_templates_json,
            )
        )
    )


async def sandboxd(
    endpoint: str,
    *,
    bearer_token: str | None = None,
    ca_certificate: str | None = None,
    client_certificate: str | None = None,
    client_key: str | None = None,
) -> Connection:
    return Connection(
        await _native._aio_sandboxd(
            endpoint, bearer_token=bearer_token, ca_certificate=ca_certificate,
            client_certificate=client_certificate, client_key=client_key,
        )
    )


async def openshell(
    endpoint: str,
    *,
    workspace: str = "default",
    workload_template: str | None = None,
    spec_protobuf: bytes | None = None,
    bearer_token: str | None = None,
    ca_certificate: str | None = None,
    client_certificate: str | None = None,
    client_key: str | None = None,
) -> OpenShell:
    return OpenShell(
        await _native._aio_openshell(
            endpoint, workspace=workspace, workload_template=workload_template,
            spec_protobuf=spec_protobuf, bearer_token=bearer_token,
            ca_certificate=ca_certificate, client_certificate=client_certificate,
            client_key=client_key,
        )
    )


async def openshell_connection(
    endpoint: str,
    sandbox: str,
    *,
    workspace: str = "default",
    bearer_token: str | None = None,
    ca_certificate: str | None = None,
    client_certificate: str | None = None,
    client_key: str | None = None,
) -> Connection:
    return Connection(
        await _native._aio_openshell_connection(
            endpoint, sandbox, workspace=workspace, bearer_token=bearer_token,
            ca_certificate=ca_certificate, client_certificate=client_certificate,
            client_key=client_key,
        )
    )


__all__ = [
    "Connection", "SandboxManager", "Sandbox", "ProcessManager", "OutputSubscription", "Process", "Output", "Terminal", "Envd",
    "Docker", "E2b", "OpenShell",
    "local", "ssh", "docker", "docker_connection", "pueue", "tmux", "envd", "e2b",
    "agent_sandbox", "sandboxd", "openshell", "openshell_connection",
]

"""Native Rust I/O on the caller's asyncio loop, with shared OpenSOL DTOs.

Each process handle has one output subscription. Read operations are serialized;
cancellation retains a reserved event until a Python consumer acknowledges it.
Closing a subscription never signals the process or deletes its environment.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Sequence

from . import _native
from ._native import (
    AsyncConnection as Connection,
    AsyncLifecycle as Lifecycle,
    ExitStatus,
    ManagerInfo,
    OutputCursor,
    OutputInfo,
    OutputPage,
    ProcessEvent,
    ProcessInfo,
    ProcessRef,
    ResourceRef,
)


class ProcessHandle(AsyncIterator[ProcessEvent]):
    """One subscription; ``wait`` discards output, ``aclose`` only detaches.

    The asyncio lock is owned by the Python coroutine, so it is released on
    cancellation. Rust keeps its single reserved event until the resumed Python
    coroutine confirms delivery without another await between those steps.
    """

    def __init__(self, native: _native.AsyncProcessHandle) -> None:
        self._native = native
        self._reader = asyncio.Lock()

    @property
    def process(self) -> ProcessRef:
        return self._native.process

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

    async def wait(self) -> ExitStatus:
        """Consume remaining output and return a confirmed native exit status."""
        while (event := await self.next_event()) is not None:
            if event.exit is not None:
                return event.exit
        return await self._native._exit()

    async def aclose(self) -> None:
        """Detach and wake blocked readers; safe to call repeatedly."""
        await self._native.aclose()

    def __aiter__(self) -> ProcessHandle:
        return self

    async def __anext__(self) -> ProcessEvent:
        event = await self.next_event()
        if event is None:
            raise StopAsyncIteration
        return event


class ProcessManager:
    """Native process operations; the application owns daemon configuration."""

    def __init__(self, native: _native.AsyncProcessManager) -> None:
        self._native = native

    @property
    def backend(self) -> str:
        return self._native.backend

    def capabilities(self) -> _native.ProcessCapabilities:
        return self._native.capabilities()

    def get(self, process: ProcessRef) -> Process:
        """Bind a resource locally; existence is checked on its next native operation."""
        return Process(self._native.get(process))

    def probe(self) -> Awaitable[ManagerInfo]:
        return self._native.probe()

    def list(self) -> Awaitable[list[ProcessInfo]]:
        return self._native.list()

    async def spawn(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: bool = False,
        pty: tuple[int, int] | None = None,
    ) -> ProcessHandle:
        return ProcessHandle(
            await self._native.spawn(argv, cwd=cwd, env=env, stdin=stdin, pty=pty)
        )

    def status(self, process: ProcessRef) -> Awaitable[ProcessInfo]:
        return self._native.status(process)

    def wait(self, process: ProcessRef, *, timeout: float | None = None) -> Awaitable[ExitStatus]:
        return self._native.wait(process, timeout=timeout)

    async def output(self, process: ProcessRef) -> ProcessHandle:
        return ProcessHandle(await self._native.output(process))

    async def reconnect(self, process: ProcessRef) -> ProcessHandle:
        return ProcessHandle(await self._native.reconnect(process))

    def send_input(self, process: ProcessRef, data: bytes) -> Awaitable[None]:
        return self._native.send_input(process, data)

    def close_stdin(self, process: ProcessRef) -> Awaitable[None]:
        return self._native.close_stdin(process)

    def signal(self, process: ProcessRef, signal: int) -> Awaitable[None]:
        return self._native.signal(process, signal)

    def resize(self, process: ProcessRef, cols: int, rows: int) -> Awaitable[None]:
        return self._native.resize(process, cols, rows)


class Process:
    """Bound native task resource; no cached status or implicit lifecycle ownership."""

    def __init__(self, native: _native.AsyncProcess) -> None:
        self._native = native

    @property
    def ref(self) -> ProcessRef:
        return self._native.ref

    @property
    def output(self) -> ProcessOutput:
        return ProcessOutput(self._native.output)

    def inspect(self) -> Awaitable[ProcessInfo]:
        return self._native.inspect()

    def wait(self, *, timeout: float | None = None) -> Awaitable[ExitStatus]:
        return self._native.wait(timeout=timeout)

    def pause(self) -> Awaitable[None]:
        return self._native.pause()

    def start(self) -> Awaitable[None]:
        return self._native.start()

    def restart(self) -> Awaitable[None]:
        return self._native.restart()

    def kill(self) -> Awaitable[None]:
        return self._native.kill()

    def remove(self) -> Awaitable[None]:
        return self._native.remove()

    def signal(self, signal: int) -> Awaitable[None]:
        return self._native.signal(signal)

    def send_input(self, data: bytes) -> Awaitable[None]:
        return self._native.send_input(data)

    def close_stdin(self) -> Awaitable[None]:
        return self._native.close_stdin()

    def resize(self, cols: int, rows: int) -> Awaitable[None]:
        return self._native.resize(cols, rows)


class ProcessOutput:
    """Independent bounded reads and subscriptions to one retained native task log."""

    def __init__(self, native: _native.AsyncProcessOutput) -> None:
        self._native = native

    def stat(self) -> Awaitable[OutputInfo]:
        return self._native.stat()

    def read(self, *, cursor: OutputCursor | None = None, limit: int = 65536) -> Awaitable[OutputPage]:
        return self._native.read(cursor=cursor, limit=limit)

    async def subscribe(self) -> ProcessHandle:
        return ProcessHandle(await self._native.subscribe())


class Envd:
    """One envd client exposing independent async connection and process APIs."""

    def __init__(self, native: _native.Envd) -> None:
        self._native = native

    @property
    def connection(self) -> Connection:
        return _native._async_connection(self._native.connection)

    @property
    def processes(self) -> ProcessManager:
        return ProcessManager(_native._async_processes(self._native.processes))


def local() -> Connection:
    return _native._async_connection(_native.local())


def ssh(
    destination: str,
    *,
    port: int | None = None,
    identity_file: str | None = None,
    config_file: str | None = None,
    program: str = "ssh",
) -> Connection:
    return _native._async_connection(
        _native.ssh(destination, port=port, identity_file=identity_file,
                    config_file=config_file, program=program)
    )


def docker(
    image: str,
    *,
    command: Sequence[str] | None = None,
    env: dict[str, str] | None = None,
    working_dir: str | None = None,
    host_config_json: str | None = None,
) -> Lifecycle:
    return _native._async_lifecycle(
        _native.docker(image, command=command, env=env, working_dir=working_dir,
                       host_config_json=host_config_json)
    )


def docker_connection(container: str) -> Connection:
    return _native._async_connection(_native.docker_connection(container))


def pueue(
    connection: Connection,
    *,
    config_file: str | None = None,
    profile: str | None = None,
    group: str | None = None,
    pueue_directory: str | None = None,
    program: str = "pueue",
) -> ProcessManager:
    manager = _native.pueue(connection._blocking(), config_file=config_file,
                            profile=profile, group=group, pueue_directory=pueue_directory,
                            program=program)
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
) -> Lifecycle:
    return _native._async_lifecycle(
        _native.e2b(api_key, template, api_endpoint=api_endpoint,
                    sandbox_timeout=sandbox_timeout, auto_pause=auto_pause,
                    env=env, metadata=metadata)
    )


async def e2b_envd(
    api_key: str,
    resource: ResourceRef,
    *,
    api_endpoint: str = "https://api.e2b.app",
    envd_endpoint: str | None = None,
) -> Envd:
    return Envd(await _native._aio_e2b_envd(
        api_key, resource, api_endpoint=api_endpoint, envd_endpoint=envd_endpoint
    ))


def agent_sandbox(
    namespace: str,
    pod_template_json: str,
    *,
    context: str | None = None,
    kubeconfig: str | None = None,
    program: str = "kubectl",
    service: bool = True,
    volume_claim_templates_json: str | None = None,
) -> Lifecycle:
    return _native._async_lifecycle(_native.agent_sandbox(
        namespace, pod_template_json, context=context, kubeconfig=kubeconfig,
        program=program, service=service,
        volume_claim_templates_json=volume_claim_templates_json,
    ))


def sandboxd(
    endpoint: str,
    *,
    bearer_token: str | None = None,
    ca_certificate: str | None = None,
    client_certificate: str | None = None,
    client_key: str | None = None,
) -> Awaitable[Connection]:
    return _native._aio_sandboxd(endpoint, bearer_token=bearer_token,
                                ca_certificate=ca_certificate,
                                client_certificate=client_certificate, client_key=client_key)


def openshell(
    endpoint: str,
    *,
    workspace: str = "default",
    workload_template: str | None = None,
    spec_protobuf: bytes | None = None,
    bearer_token: str | None = None,
    ca_certificate: str | None = None,
    client_certificate: str | None = None,
    client_key: str | None = None,
) -> Awaitable[Lifecycle]:
    return _native._aio_openshell(endpoint, workspace=workspace,
                                 workload_template=workload_template,
                                 spec_protobuf=spec_protobuf, bearer_token=bearer_token,
                                 ca_certificate=ca_certificate,
                                 client_certificate=client_certificate, client_key=client_key)


def openshell_connection(
    endpoint: str,
    sandbox: str,
    *,
    workspace: str = "default",
    bearer_token: str | None = None,
    ca_certificate: str | None = None,
    client_certificate: str | None = None,
    client_key: str | None = None,
) -> Awaitable[Connection]:
    return _native._aio_openshell_connection(endpoint, sandbox, workspace=workspace,
                                            bearer_token=bearer_token,
                                            ca_certificate=ca_certificate,
                                            client_certificate=client_certificate,
                                            client_key=client_key)


__all__ = [
    "Connection", "Lifecycle", "ProcessManager", "ProcessHandle", "Process", "ProcessOutput", "Envd",
    "local", "ssh", "docker", "docker_connection", "pueue", "envd", "e2b",
    "e2b_envd", "agent_sandbox", "sandboxd", "openshell", "openshell_connection",
]

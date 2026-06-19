from __future__ import annotations

import socket
from typing import cast

import pytest
from grpclib.client import Channel
from grpclib.const import Status
from grpclib.exceptions import GRPCError
from grpclib.server import Server

import shell_session_manager.shellctl.server.grpc as grpc_module
from shell_session_manager.shellctl.client import ShellctlClient, ShellctlClientError
from shell_session_manager.shellctl.client.common import decode_grpc_error
from shell_session_manager.shellctl.proto.v1 import shellctl_pb2 as pb
from shell_session_manager.shellctl.proto.v1.shellctl_grpc import ShellctlStub
from shell_session_manager.shellctl.server.auth import AuthVerifier
from shell_session_manager.shellctl.server.config import ShellctlConfig
from shell_session_manager.shellctl.server.error_codec import ErrorCodec
from shell_session_manager.shellctl.server.errors import ShellctlServerError
from shell_session_manager.shellctl.server.grpc import ShellctlGrpcService
from shell_session_manager.shellctl.server.service import ShellctlService
from shell_session_manager.shellctl.server.transport import ShellctlEndpointController
from shell_session_manager.shellctl.shared import (
    DeleteJobResponse,
    InputJobRequest,
    JobInfo,
    JobResult,
    JobStatusName,
    JobStatusView,
    ListJobsResponse,
    RunJobRequest,
    TerminateJobRequest,
    WaitJobRequest,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeGrpcBackend:
    def __init__(self) -> None:
        self.run_requests: list[RunJobRequest] = []
        self.wait_requests: list[tuple[str, WaitJobRequest]] = []
        self.input_requests: list[tuple[str, InputJobRequest]] = []
        self.tail_requests: list[tuple[str, int]] = []
        self.list_requests: list[tuple[JobStatusName | None, int]] = []
        self.terminate_requests: list[tuple[str, TerminateJobRequest]] = []
        self.delete_requests: list[tuple[str, bool, float]] = []
        self.status_requests: list[str] = []
        self.status_error: Exception | None = None

    async def run_job(self, request: RunJobRequest) -> JobResult:
        self.run_requests.append(request)
        return _job_result(request_output="run")

    async def wait_job(self, job_id: str, request: WaitJobRequest) -> JobResult:
        self.wait_requests.append((job_id, request))
        return _job_result(job_id=job_id, request_output="wait")

    async def tail_job(self, job_id: str, *, output_limit: int) -> JobResult:
        self.tail_requests.append((job_id, output_limit))
        return _job_result(job_id=job_id, request_output="tail")

    async def get_job_status(self, job_id: str) -> JobStatusView:
        self.status_requests.append(job_id)
        if self.status_error is not None:
            raise self.status_error
        return _job_status_view(job_id)

    async def list_jobs(
        self, *, status: JobStatusName | None = None, limit: int
    ) -> ListJobsResponse:
        self.list_requests.append((status, limit))
        return ListJobsResponse(
            jobs=[
                JobInfo(
                    job_id="job-1",
                    status=JobStatusName.RUNNING,
                    created_at="2026-05-21T15:30:12Z",
                    started_at="2026-05-21T15:30:13Z",
                    ended_at=None,
                )
            ]
        )

    async def send_input(self, job_id: str, request: InputJobRequest) -> JobResult:
        self.input_requests.append((job_id, request))
        return _job_result(job_id=job_id, request_output=request.text)

    async def terminate_job(
        self, job_id: str, request: TerminateJobRequest
    ) -> JobStatusView:
        self.terminate_requests.append((job_id, request))
        return _job_status_view(job_id, status=JobStatusName.TERMINATED, done=True)

    async def delete_job(
        self,
        job_id: str,
        *,
        force: bool,
        grace_seconds: float,
    ) -> DeleteJobResponse:
        self.delete_requests.append((job_id, force, grace_seconds))
        return DeleteJobResponse(job_id=job_id, deleted=True)


@pytest.mark.anyio
async def test_grpc_health_is_public_and_protected_calls_require_auth() -> None:
    backend = FakeGrpcBackend()
    server, port = await _start_shellctl_grpc_server(backend, token="secret")

    try:
        async with ShellctlClient(f"grpc://127.0.0.1:{port}") as unauthenticated:
            assert await unauthenticated.healthz() == {"status": "ok"}
            with pytest.raises(ShellctlClientError) as exc_info:
                await unauthenticated.status("job-1")

        assert exc_info.value.status_code == 401
        assert exc_info.value.code == "unauthorized"

        async with ShellctlClient(f"grpc://127.0.0.1:{port}", token="secret") as client:
            view = await client.status("job-1")

        assert view.job_id == "job-1"
        assert backend.status_requests == ["job-1"]
    finally:
        await _stop_server(server)


@pytest.mark.anyio
async def test_grpc_protected_rpc_allows_missing_metadata_when_auth_disabled() -> None:
    backend = FakeGrpcBackend()
    server, port = await _start_shellctl_grpc_server(backend, token=None)

    try:
        async with ShellctlClient(f"grpc://127.0.0.1:{port}") as client:
            view = await client.status("job-1")

        assert view.job_id == "job-1"
        assert backend.status_requests == ["job-1"]
    finally:
        await _stop_server(server)


@pytest.mark.anyio
async def test_grpc_client_injects_defaults_and_shared_request_shapes() -> None:
    backend = FakeGrpcBackend()
    server, port = await _start_shellctl_grpc_server(backend, token="secret")

    try:
        async with ShellctlClient(
            f"grpc://127.0.0.1:{port}",
            token="secret",
            output_limit=4096,
            idle_flush_seconds=0.25,
        ) as client:
            run_result = await client.run(
                "printf ready\\n",
                cwd="/tmp",
                env={"HELLO": "world"},
                timeout=12,
            )
            wait_result = await client.wait("job-1", offset=3, timeout=4)
            input_result = await client.input("job-1", "ls\n", offset=5, timeout=6)
            tail_result = await client.tail("job-1")
            jobs = await client.list_jobs(status="running", limit=7)
            terminated = await client.terminate("job-1", grace_seconds=1.5)
            deleted = await client.delete("job-1", force=True, grace_seconds=2.5)

        assert run_result.output == "run"
        assert wait_result.output == "wait"
        assert input_result.output == "ls\n"
        assert tail_result.output == "tail"
        assert jobs[0].job_id == "job-1"
        assert terminated.status is JobStatusName.TERMINATED
        assert deleted.deleted is True

        run_request = backend.run_requests[0]
        assert run_request.output_limit == 4096
        assert run_request.idle_flush_seconds == 0.25
        assert run_request.env == {"HELLO": "world"}

        wait_job_id, wait_request = backend.wait_requests[0]
        assert wait_job_id == "job-1"
        assert wait_request.offset == 3
        assert wait_request.timeout == 4
        assert wait_request.output_limit == 4096
        assert wait_request.idle_flush_seconds == 0.25

        input_job_id, input_request = backend.input_requests[0]
        assert input_job_id == "job-1"
        assert input_request.offset == 5
        assert input_request.timeout == 6
        assert input_request.output_limit == 4096
        assert input_request.idle_flush_seconds == 0.25

        assert backend.tail_requests == [("job-1", 4096)]
        assert backend.list_requests == [(JobStatusName.RUNNING, 7)]
        assert backend.terminate_requests[0][1].grace_seconds == 1.5
        assert backend.delete_requests == [("job-1", True, 2.5)]
    finally:
        await _stop_server(server)


@pytest.mark.anyio
async def test_grpc_server_translates_malformed_protobuf_request_to_invalid_request() -> (
    None
):
    backend = FakeGrpcBackend()
    server, port = await _start_shellctl_grpc_server(backend, token=None)

    channel = Channel(host="127.0.0.1", port=port)
    stub = ShellctlStub(channel)
    request = pb.RunJobRequest(script="printf ready\n")
    request.env[""] = "bad"

    try:
        with pytest.raises(GRPCError) as exc_info:
            await stub.RunJob(request)
    finally:
        channel.close()
        await _stop_server(server)

    error = decode_grpc_error(exc_info.value)
    assert error.status_code == 400
    assert error.code == "invalid_request"
    assert "env" in error.message


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected_status", "expected_code"),
    [
        (ShellctlServerError(404, "job_not_found", "missing"), 404, "job_not_found"),
        (
            ShellctlServerError(409, "job_not_running", "already done"),
            409,
            "job_not_running",
        ),
        (ShellctlServerError(500, "tmux_failed", "boom"), 500, "tmux_failed"),
    ],
)
async def test_grpc_structured_errors_round_trip_to_client_exception(
    error: ShellctlServerError,
    expected_status: int,
    expected_code: str,
) -> None:
    backend = FakeGrpcBackend()
    backend.status_error = error
    server, port = await _start_shellctl_grpc_server(backend, token="secret")

    try:
        async with ShellctlClient(f"grpc://127.0.0.1:{port}", token="secret") as client:
            with pytest.raises(ShellctlClientError) as exc_info:
                await client.status("job-1")

        assert exc_info.value.status_code == expected_status
        assert exc_info.value.code == expected_code
        assert exc_info.value.message == error.message
    finally:
        await _stop_server(server)


def test_decode_grpc_error_falls_back_for_plain_messages() -> None:
    error = decode_grpc_error(GRPCError(Status.INTERNAL, "plain grpc failure"))

    assert error.status_code == 500
    assert error.code == "grpc_error"
    assert error.message == "plain grpc failure"


@pytest.mark.anyio
async def test_grpc_client_list_jobs_invalid_limit_raises_client_error() -> None:
    async with ShellctlClient("grpc://127.0.0.1:1") as client:
        with pytest.raises(ShellctlClientError, match="invalid_request") as exc_info:
            await client.list_jobs(limit=-1)

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "invalid_request"


@pytest.mark.anyio
async def test_grpc_client_tail_invalid_output_limit_raises_client_error() -> None:
    async with ShellctlClient("grpc://127.0.0.1:1", output_limit=-1) as client:
        with pytest.raises(ShellctlClientError, match="invalid_request") as exc_info:
            await client.tail("job-1")

    assert exc_info.value.status_code == 400
    assert exc_info.value.code == "invalid_request"


@pytest.mark.anyio
async def test_run_grpc_server_shutdowns_cleanly_when_bind_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created_servers: list[FakeServer] = []
    lifecycle = FakeManagedService()

    monkeypatch.setattr(
        grpc_module,
        "Server",
        lambda handlers: FakeServer(handlers, created_servers),
    )

    with pytest.raises(OSError, match="already in use"):
        await grpc_module.run_grpc_server(
            ShellctlConfig(listen="127.0.0.1:8766"),
            service=cast(ShellctlService, lifecycle),
        )

    assert lifecycle.initialized is True
    assert lifecycle.gc_started is True
    assert lifecycle.pipe_monitor_started is True
    assert lifecycle.shutdown_called is True
    assert len(created_servers) == 1
    assert created_servers[0].closed is False
    assert created_servers[0].wait_closed_called is False


async def _start_shellctl_grpc_server(
    backend: FakeGrpcBackend,
    *,
    token: str | None,
) -> tuple[Server, int]:
    controller = ShellctlEndpointController(cast(ShellctlService, backend))
    service = ShellctlGrpcService(
        controller,
        auth=AuthVerifier(token),
        errors=ErrorCodec(),
    )
    server = Server([service])
    port = _free_tcp_port()
    await server.start(host="127.0.0.1", port=port)
    return server, port


async def _stop_server(server: Server) -> None:
    server.close()
    await server.wait_closed()


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class FakeManagedService:
    def __init__(self) -> None:
        self.initialized = False
        self.gc_started = False
        self.pipe_monitor_started = False
        self.shutdown_called = False

    async def initialize(self) -> None:
        self.initialized = True

    def start_background_gc(self) -> None:
        self.gc_started = True

    def start_background_pipe_monitor(self) -> None:
        self.pipe_monitor_started = True

    async def shutdown(self) -> None:
        self.shutdown_called = True


class FakeServer:
    def __init__(self, _handlers: object, created_servers: list[FakeServer]) -> None:
        self.closed = False
        self.wait_closed_called = False
        created_servers.append(self)

    async def start(self, *, host: str, port: int) -> None:
        del host, port
        raise OSError("already in use")

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.wait_closed_called = True


def _job_result(job_id: str = "job-1", *, request_output: str) -> JobResult:
    return JobResult(
        job_id=job_id,
        done=False,
        status=JobStatusName.RUNNING,
        exit_code=None,
        output_path=f"/tmp/{job_id}.log",
        output=request_output,
        offset=len(request_output.encode("utf-8")),
        truncated=False,
    )


def _job_status_view(
    job_id: str,
    *,
    status: JobStatusName = JobStatusName.RUNNING,
    done: bool = False,
) -> JobStatusView:
    return JobStatusView(
        job_id=job_id,
        status=status,
        done=done,
        exit_code=0 if done else None,
        created_at="2026-05-21T15:30:12Z",
        started_at="2026-05-21T15:30:13Z",
        ended_at="2026-05-21T15:30:14Z" if done else None,
        offset=7,
    )

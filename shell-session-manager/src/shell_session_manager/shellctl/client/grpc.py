# pyright: reportAttributeAccessIssue=false

"""grpclib transport implementation for the shellctl SDK façade."""

from __future__ import annotations

from grpclib.client import Channel
from grpclib.exceptions import GRPCError

from shell_session_manager.shellctl.client.common import (
    ShellctlClientDefaults,
    ShellctlClientError,
    auth_header,
    decode_grpc_error,
)
from shell_session_manager.shellctl.proto.v1 import shellctl_pb2 as pb
from shell_session_manager.shellctl.proto.v1.shellctl_grpc import ShellctlStub
from shell_session_manager.shellctl.shared import (
    DeleteJobResponse,
    HealthResponse,
    InputJobRequest,
    JobResult,
    JobStatusName,
    JobStatusView,
    ListJobsResponse,
    RunJobRequest,
    TerminateJobRequest,
    WaitJobRequest,
)
from shell_session_manager.shellctl.shared import (
    protobuf as proto_codec,
)


class GrpcShellctlTransport:
    """grpclib-based shellctl transport used by `grpc://` and `grpcs://` URLs.

    The transport owns the channel unless a prebuilt one is injected, which
    keeps tests free to provide in-process channels while normal callers get a
    simple host/port constructor.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        defaults: ShellctlClientDefaults,
        ssl: object | None = None,
        channel: Channel | None = None,
    ) -> None:
        self._defaults = defaults
        self._owns_channel = channel is None
        self._channel = channel or Channel(host=host, port=port, ssl=ssl)
        self._stub = ShellctlStub(self._channel)

    async def close(self) -> None:
        """Close the owned grpclib channel, if any."""

        if self._owns_channel:
            self._channel.close()

    async def healthz(self) -> HealthResponse:
        """Call the public shellctl health RPC without auth metadata."""

        try:
            response = await self._stub.Health(pb.HealthRequest())
        except GRPCError as exc:
            raise decode_grpc_error(exc) from exc
        return proto_codec.health_response_from_protobuf(response)

    async def run(self, request: RunJobRequest) -> JobResult:
        """Call the `RunJob` RPC."""

        return await self._call_job_result(
            self._stub.RunJob,
            proto_codec.run_job_request_to_protobuf(request),
        )

    async def wait(self, job_id: str, request: WaitJobRequest) -> JobResult:
        """Call the `WaitJob` RPC."""

        return await self._call_job_result(
            self._stub.WaitJob,
            proto_codec.wait_job_request_to_protobuf(job_id, request),
        )

    async def status(self, job_id: str) -> JobStatusView:
        """Call the `GetJobStatus` RPC."""

        try:
            response = await self._stub.GetJobStatus(
                proto_codec.get_job_status_request_to_protobuf(job_id),
                metadata=self._metadata(),
            )
        except GRPCError as exc:
            raise decode_grpc_error(exc) from exc
        return proto_codec.job_status_view_from_protobuf(response)

    async def list_jobs(self, *, status: str | None, limit: int) -> ListJobsResponse:
        """Call the `ListJobs` RPC."""

        try:
            status_filter = None if status is None else JobStatusName(status)
        except ValueError as exc:
            raise ShellctlClientError(400, "invalid_request", str(exc)) from exc
        try:
            response = await self._stub.ListJobs(
                proto_codec.list_jobs_request_to_protobuf(
                    status=status_filter,
                    limit=limit,
                ),
                metadata=self._metadata(),
            )
        except ValueError as exc:
            raise ShellctlClientError(400, "invalid_request", str(exc)) from exc
        except GRPCError as exc:
            raise decode_grpc_error(exc) from exc
        return proto_codec.list_jobs_response_from_protobuf(response)

    async def input(self, job_id: str, request: InputJobRequest) -> JobResult:
        """Call the `SendInput` RPC."""

        return await self._call_job_result(
            self._stub.SendInput,
            proto_codec.input_job_request_to_protobuf(job_id, request),
        )

    async def tail(self, job_id: str, *, output_limit: int) -> JobResult:
        """Call the `TailJob` RPC."""

        try:
            request = proto_codec.tail_job_request_to_protobuf(
                job_id,
                output_limit=output_limit,
            )
        except ValueError as exc:
            raise ShellctlClientError(400, "invalid_request", str(exc)) from exc
        return await self._call_job_result(self._stub.TailJob, request)

    async def terminate(
        self, job_id: str, request: TerminateJobRequest
    ) -> JobStatusView:
        """Call the `TerminateJob` RPC."""

        try:
            response = await self._stub.TerminateJob(
                proto_codec.terminate_job_request_to_protobuf(job_id, request),
                metadata=self._metadata(),
            )
        except GRPCError as exc:
            raise decode_grpc_error(exc) from exc
        return proto_codec.job_status_view_from_protobuf(response)

    async def delete(
        self,
        job_id: str,
        *,
        force: bool,
        grace_seconds: float | None,
    ) -> DeleteJobResponse:
        """Call the `DeleteJob` RPC."""

        try:
            response = await self._stub.DeleteJob(
                proto_codec.delete_job_request_to_protobuf(
                    job_id,
                    force=force,
                    grace_seconds=grace_seconds,
                ),
                metadata=self._metadata(),
            )
        except GRPCError as exc:
            raise decode_grpc_error(exc) from exc
        return proto_codec.delete_job_response_from_protobuf(response)

    async def _call_job_result(self, method: object, request: object) -> JobResult:
        try:
            response = await method(request, metadata=self._metadata())  # type: ignore[operator]
        except GRPCError as exc:
            raise decode_grpc_error(exc) from exc
        return proto_codec.job_result_from_protobuf(response)

    def _metadata(self) -> dict[str, str]:
        return auth_header(self._defaults.token)


__all__ = ["GrpcShellctlTransport"]

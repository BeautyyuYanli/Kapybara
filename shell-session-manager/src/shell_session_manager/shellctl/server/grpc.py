# pyright: reportAttributeAccessIssue=false

"""grpclib-based gRPC transport for the shellctl service.

This module keeps the protocol layer deliberately thin: auth, protobuf/Pydantic
conversion, and error translation happen here, while `ShellctlService` remains
the only place that owns job lifecycle behavior.

Protected RPCs read bearer auth from gRPC metadata key `authorization`, matching
the HTTP `Authorization: Bearer ...` contract. `Health` is intentionally the only
RPC that bypasses that auth check.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import TypeVar, cast

import anyio
from grpclib.server import Server, Stream
from pydantic import ValidationError

from shell_session_manager.shellctl.proto.v1 import shellctl_grpc
from shell_session_manager.shellctl.proto.v1 import shellctl_pb2 as pb
from shell_session_manager.shellctl.server.auth import AuthVerifier
from shell_session_manager.shellctl.server.config import ShellctlConfig
from shell_session_manager.shellctl.server.error_codec import ErrorCodec
from shell_session_manager.shellctl.server.listen import parse_listen
from shell_session_manager.shellctl.server.service import ShellctlService
from shell_session_manager.shellctl.server.transport import ShellctlEndpointController
from shell_session_manager.shellctl.shared import protobuf as proto_codec

_RequestT = TypeVar("_RequestT")
_DecodedT = TypeVar("_DecodedT")
_ResponseT = TypeVar("_ResponseT")
_EncodedT = TypeVar("_EncodedT")


class ShellctlGrpcService(shellctl_grpc.ShellctlBase):
    """grpclib service that forwards unary RPCs into `ShellctlEndpointController`."""

    def __init__(
        self,
        controller: ShellctlEndpointController,
        *,
        auth: AuthVerifier,
        errors: ErrorCodec | None = None,
    ) -> None:
        self._controller = controller
        self._auth = auth
        self._errors = errors or ErrorCodec()

    async def Health(self, stream: Stream[pb.HealthRequest, pb.HealthResponse]) -> None:
        await self._handle_unary(
            stream,
            auth_required=False,
            decode=lambda _request: None,
            invoke=lambda _decoded: self._controller.health(),
            encode=proto_codec.health_response_to_protobuf,
        )

    async def RunJob(self, stream: Stream[pb.RunJobRequest, pb.JobResult]) -> None:
        await self._handle_unary(
            stream,
            decode=proto_codec.run_job_request_from_protobuf,
            invoke=self._controller.run_job,
            encode=proto_codec.job_result_to_protobuf,
        )

    async def WaitJob(self, stream: Stream[pb.WaitJobRequest, pb.JobResult]) -> None:
        await self._handle_unary(
            stream,
            decode=proto_codec.wait_job_request_from_protobuf,
            invoke=lambda decoded: self._controller.wait_job(*decoded),
            encode=proto_codec.job_result_to_protobuf,
        )

    async def TailJob(self, stream: Stream[pb.TailJobRequest, pb.JobResult]) -> None:
        await self._handle_unary(
            stream,
            decode=proto_codec.tail_job_request_from_protobuf,
            invoke=lambda decoded: self._controller.tail_job(
                decoded[0], output_limit=decoded[1]
            ),
            encode=proto_codec.job_result_to_protobuf,
        )

    async def GetJobStatus(
        self, stream: Stream[pb.GetJobStatusRequest, pb.JobStatusView]
    ) -> None:
        await self._handle_unary(
            stream,
            decode=proto_codec.get_job_status_request_from_protobuf,
            invoke=self._controller.get_job_status,
            encode=proto_codec.job_status_view_to_protobuf,
        )

    async def ListJobs(
        self, stream: Stream[pb.ListJobsRequest, pb.ListJobsResponse]
    ) -> None:
        await self._handle_unary(
            stream,
            decode=proto_codec.list_jobs_request_from_protobuf,
            invoke=lambda decoded: self._controller.list_jobs(
                status=decoded[0], limit=decoded[1]
            ),
            encode=proto_codec.list_jobs_response_to_protobuf,
        )

    async def SendInput(self, stream: Stream[pb.InputJobRequest, pb.JobResult]) -> None:
        await self._handle_unary(
            stream,
            decode=proto_codec.input_job_request_from_protobuf,
            invoke=lambda decoded: self._controller.send_input(*decoded),
            encode=proto_codec.job_result_to_protobuf,
        )

    async def TerminateJob(
        self, stream: Stream[pb.TerminateJobRequest, pb.JobStatusView]
    ) -> None:
        await self._handle_unary(
            stream,
            decode=proto_codec.terminate_job_request_from_protobuf,
            invoke=lambda decoded: self._controller.terminate_job(*decoded),
            encode=proto_codec.job_status_view_to_protobuf,
        )

    async def DeleteJob(
        self, stream: Stream[pb.DeleteJobRequest, pb.DeleteJobResponse]
    ) -> None:
        await self._handle_unary(
            stream,
            decode=proto_codec.delete_job_request_from_protobuf,
            invoke=lambda decoded: self._controller.delete_job(
                decoded[0], force=decoded[1], grace_seconds=decoded[2]
            ),
            encode=proto_codec.delete_job_response_to_protobuf,
        )

    async def _handle_unary(
        self,
        stream: Stream[_RequestT, _EncodedT],
        *,
        decode: Callable[[_RequestT], _DecodedT],
        invoke: Callable[[_DecodedT], Awaitable[_ResponseT]],
        encode: Callable[[_ResponseT], _EncodedT],
        auth_required: bool = True,
    ) -> None:
        try:
            if auth_required:
                self._auth.verify_authorization_header(
                    _authorization_from_metadata(stream.metadata)
                )
            request = await stream.recv_message()
            if request is None:
                raise RuntimeError("missing request message")
            response = await invoke(decode(request))
            await stream.send_message(encode(response))
        except (RuntimeError, ValidationError, ValueError, TypeError) as exc:
            raise self._errors.to_grpc_error(exc) from exc


async def run_grpc_server(
    config: ShellctlConfig,
    *,
    service: ShellctlService | None = None,
) -> None:
    """Run the shellctl gRPC server until cancelled.

    This mirrors the HTTP lifespan behavior: initialize the service, start the
    GC and pipe-monitor tasks, then keep the grpclib server alive until the task
    is cancelled or the process exits.
    """

    resolved_service = service or ShellctlService(config)
    controller = ShellctlEndpointController(resolved_service)
    grpc_service = ShellctlGrpcService(
        controller,
        auth=AuthVerifier.from_config(config),
    )
    host, port = parse_listen(config.listen)
    server = Server([grpc_service])

    started = False
    try:
        await resolved_service.initialize()
        resolved_service.start_background_gc()
        resolved_service.start_background_pipe_monitor()
        await server.start(host=host, port=port)
        started = True
        await anyio.sleep_forever()
    finally:
        if started:
            server.close()
            await server.wait_closed()
        await resolved_service.shutdown()


def _authorization_from_metadata(
    metadata: Mapping[str, str | bytes] | Iterable[tuple[str, str | bytes]] | None,
) -> str | None:
    """Extract the HTTP-style bearer header value from grpclib metadata.

    grpclib may expose metadata either as a mapping or as an iterable of
    key/value pairs. shellctl treats metadata key matching as case-insensitive,
    reads auth only from `authorization`, and decodes byte values to UTF-8 so the
    result can be compared with the same `Bearer <token>` string used by HTTP.
    Callers skip this helper only for the public `Health` RPC.
    """

    if metadata is None:
        return None
    items = metadata.items() if isinstance(metadata, Mapping) else metadata
    for key, value in cast(Iterable[tuple[str, str | bytes]], items):
        if key.lower() != "authorization":
            continue
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return value
    return None


__all__ = ["ShellctlGrpcService", "run_grpc_server"]

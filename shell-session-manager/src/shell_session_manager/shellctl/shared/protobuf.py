# pyright: reportAttributeAccessIssue=false

"""Conversion helpers between shellctl's canonical DTOs and protobuf messages.

`ShellctlService` and the Python SDK continue to treat the Pydantic models in
`shell_session_manager.shellctl.shared.schemas` as the source of truth. The gRPC
layer must therefore convert at the transport boundary instead of threading
protobuf message objects through the service.
"""

from __future__ import annotations

from typing import Final

from google.protobuf.message import Message

from shell_session_manager.shellctl.proto.v1 import shellctl_pb2 as pb
from shell_session_manager.shellctl.shared.constants import (
    DEFAULT_IDLE_FLUSH_SECONDS,
    DEFAULT_LIST_LIMIT,
    DEFAULT_OUTPUT_LIMIT_BYTES,
    DEFAULT_TERMINATE_GRACE_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    MAX_LIST_LIMIT,
    MAX_OUTPUT_LIMIT_BYTES,
)
from shell_session_manager.shellctl.shared.schemas import (
    DeleteJobResponse,
    HealthResponse,
    InputJobRequest,
    JobInfo,
    JobResult,
    JobStatusName,
    JobStatusView,
    ListJobsResponse,
    RunJobRequest,
    TerminalSize,
    TerminateJobRequest,
    WaitJobRequest,
)

_JOB_STATUS_TO_PROTO: Final[dict[JobStatusName, int]] = {
    JobStatusName.CREATED: pb.JOB_STATUS_CREATED,
    JobStatusName.STARTING: pb.JOB_STATUS_STARTING,
    JobStatusName.RUNNING: pb.JOB_STATUS_RUNNING,
    JobStatusName.EXITED: pb.JOB_STATUS_EXITED,
    JobStatusName.TERMINATED: pb.JOB_STATUS_TERMINATED,
    JobStatusName.FAILED: pb.JOB_STATUS_FAILED,
    JobStatusName.LOST: pb.JOB_STATUS_LOST,
}
_PROTO_TO_JOB_STATUS: Final[dict[int, JobStatusName]] = {
    value: key for key, value in _JOB_STATUS_TO_PROTO.items()
}


def job_status_to_protobuf(status: JobStatusName) -> int:
    """Encode a canonical lifecycle enum into the protobuf wire enum."""

    return _JOB_STATUS_TO_PROTO[status]


def job_status_from_protobuf(status: int) -> JobStatusName:
    """Decode a required protobuf lifecycle enum.

    Raises:
        ValueError: If the protobuf value is unknown or unspecified.
    """

    if status == pb.JOB_STATUS_UNSPECIFIED:
        raise ValueError("job status must not be unspecified here")
    try:
        return _PROTO_TO_JOB_STATUS[status]
    except KeyError as exc:  # pragma: no cover - corrupted client/server schema
        raise ValueError(f"unknown protobuf job status: {status}") from exc


def optional_job_status_from_protobuf(status: int) -> JobStatusName | None:
    """Decode an optional protobuf lifecycle filter.

    `JOB_STATUS_UNSPECIFIED` is reserved as the filter sentinel meaning "no
    status filter", mirroring the HTTP endpoint's `status=None` behavior.
    """

    if status == pb.JOB_STATUS_UNSPECIFIED:
        return None
    return job_status_from_protobuf(status)


def terminal_size_to_protobuf(size: TerminalSize) -> pb.TerminalSize:
    """Convert a terminal geometry DTO into a protobuf message."""

    return pb.TerminalSize(cols=size.cols, rows=size.rows)


def terminal_size_from_protobuf(message: pb.TerminalSize) -> TerminalSize:
    """Convert a protobuf terminal geometry into the canonical DTO."""

    return TerminalSize(cols=message.cols, rows=message.rows)


def run_job_request_to_protobuf(request: RunJobRequest) -> pb.RunJobRequest:
    """Encode a run request DTO for the gRPC client transport."""

    message = pb.RunJobRequest(script=request.script)
    if request.cwd is not None:
        message.cwd = request.cwd
    if request.env:
        message.env.update(request.env)
    if request.terminal is not None:
        message.terminal.CopyFrom(terminal_size_to_protobuf(request.terminal))
    message.timeout = request.timeout
    message.output_limit = request.output_limit
    message.idle_flush_seconds = request.idle_flush_seconds
    return message


def run_job_request_from_protobuf(message: pb.RunJobRequest) -> RunJobRequest:
    """Decode a gRPC run request into the canonical DTO.

    Empty and absent `env` both become `None` because the service treats both as
    "no overlay" already; protobuf maps cannot preserve that distinction.
    """

    return RunJobRequest(
        script=message.script,
        cwd=_optional_string_field(message, "cwd"),
        env=dict(message.env) or None,
        terminal=(
            terminal_size_from_protobuf(message.terminal)
            if message.HasField("terminal")
            else None
        ),
        timeout=_optional_float_field(message, "timeout", DEFAULT_TIMEOUT_SECONDS),
        output_limit=_optional_uint32_field(
            message, "output_limit", DEFAULT_OUTPUT_LIMIT_BYTES
        ),
        idle_flush_seconds=_optional_float_field(
            message, "idle_flush_seconds", DEFAULT_IDLE_FLUSH_SECONDS
        ),
    )


def wait_job_request_to_protobuf(
    job_id: str, request: WaitJobRequest
) -> pb.WaitJobRequest:
    """Encode a wait request plus path parameter for gRPC transport."""

    message = pb.WaitJobRequest(job_id=job_id, offset=request.offset)
    message.timeout = request.timeout
    message.output_limit = request.output_limit
    message.idle_flush_seconds = request.idle_flush_seconds
    return message


def wait_job_request_from_protobuf(
    message: pb.WaitJobRequest,
) -> tuple[str, WaitJobRequest]:
    """Decode a gRPC wait request into the service's `(job_id, DTO)` shape."""

    return message.job_id, WaitJobRequest(
        offset=message.offset,
        timeout=_optional_float_field(message, "timeout", DEFAULT_TIMEOUT_SECONDS),
        output_limit=_optional_uint32_field(
            message, "output_limit", DEFAULT_OUTPUT_LIMIT_BYTES
        ),
        idle_flush_seconds=_optional_float_field(
            message, "idle_flush_seconds", DEFAULT_IDLE_FLUSH_SECONDS
        ),
    )


def tail_job_request_to_protobuf(
    job_id: str, *, output_limit: int
) -> pb.TailJobRequest:
    """Encode a tail request for the gRPC client transport."""

    message = pb.TailJobRequest(job_id=job_id)
    message.output_limit = output_limit
    return message


def tail_job_request_from_protobuf(message: pb.TailJobRequest) -> tuple[str, int]:
    """Decode a gRPC tail request into the service's argument shape.

    Tail and list RPCs carry primitive query-style fields rather than full
    Pydantic request models, so the protobuf boundary must re-apply the same
    bounds enforced by the HTTP layer before reaching the service.
    """

    return message.job_id, _validate_output_limit(
        _optional_uint32_field(message, "output_limit", DEFAULT_OUTPUT_LIMIT_BYTES)
    )


def get_job_status_request_to_protobuf(job_id: str) -> pb.GetJobStatusRequest:
    """Encode a job status lookup request."""

    return pb.GetJobStatusRequest(job_id=job_id)


def get_job_status_request_from_protobuf(message: pb.GetJobStatusRequest) -> str:
    """Decode the gRPC status lookup request into its path parameter."""

    return message.job_id


def list_jobs_request_to_protobuf(
    *, status: JobStatusName | None, limit: int
) -> pb.ListJobsRequest:
    """Encode list query arguments for gRPC transport."""

    message = pb.ListJobsRequest(
        status=(
            pb.JOB_STATUS_UNSPECIFIED
            if status is None
            else job_status_to_protobuf(status)
        )
    )
    message.limit = limit
    return message


def list_jobs_request_from_protobuf(
    message: pb.ListJobsRequest,
) -> tuple[JobStatusName | None, int]:
    """Decode list query arguments from protobuf into service-friendly values.

    The returned limit matches the HTTP route's query validation so gRPC cannot
    bypass the shared list-size cap by sending out-of-range primitive values.
    """

    return optional_job_status_from_protobuf(message.status), _validate_list_limit(
        _optional_uint32_field(message, "limit", DEFAULT_LIST_LIMIT)
    )


def input_job_request_to_protobuf(
    job_id: str, request: InputJobRequest
) -> pb.InputJobRequest:
    """Encode an input request plus job identifier for gRPC transport."""

    message = pb.InputJobRequest(
        job_id=job_id, text=request.text, offset=request.offset
    )
    message.timeout = request.timeout
    message.output_limit = request.output_limit
    message.idle_flush_seconds = request.idle_flush_seconds
    return message


def input_job_request_from_protobuf(
    message: pb.InputJobRequest,
) -> tuple[str, InputJobRequest]:
    """Decode a gRPC input request into the service's `(job_id, DTO)` shape."""

    return message.job_id, InputJobRequest(
        text=message.text,
        offset=message.offset,
        timeout=_optional_float_field(message, "timeout", DEFAULT_TIMEOUT_SECONDS),
        output_limit=_optional_uint32_field(
            message, "output_limit", DEFAULT_OUTPUT_LIMIT_BYTES
        ),
        idle_flush_seconds=_optional_float_field(
            message, "idle_flush_seconds", DEFAULT_IDLE_FLUSH_SECONDS
        ),
    )


def terminate_job_request_to_protobuf(
    job_id: str, request: TerminateJobRequest
) -> pb.TerminateJobRequest:
    """Encode a terminate request plus job identifier for gRPC transport."""

    message = pb.TerminateJobRequest(job_id=job_id)
    message.grace_seconds = request.grace_seconds
    return message


def terminate_job_request_from_protobuf(
    message: pb.TerminateJobRequest,
) -> tuple[str, TerminateJobRequest]:
    """Decode a terminate request into the service's argument shape."""

    return message.job_id, TerminateJobRequest(
        grace_seconds=_optional_float_field(
            message, "grace_seconds", DEFAULT_TERMINATE_GRACE_SECONDS
        )
    )


def delete_job_request_to_protobuf(
    job_id: str,
    *,
    force: bool,
    grace_seconds: float | None,
) -> pb.DeleteJobRequest:
    """Encode delete query arguments for gRPC transport."""

    message = pb.DeleteJobRequest(job_id=job_id, force=force)
    if grace_seconds is not None:
        message.grace_seconds = grace_seconds
    return message


def delete_job_request_from_protobuf(
    message: pb.DeleteJobRequest,
) -> tuple[str, bool, float]:
    """Decode a gRPC delete request into the service's argument shape."""

    return (
        message.job_id,
        message.force,
        _optional_float_field(
            message, "grace_seconds", DEFAULT_TERMINATE_GRACE_SECONDS
        ),
    )


def job_result_to_protobuf(result: JobResult) -> pb.JobResult:
    """Encode a job result DTO for gRPC responses."""

    message = pb.JobResult(
        job_id=result.job_id,
        done=result.done,
        status=job_status_to_protobuf(result.status),
        output_path=result.output_path,
        output=result.output,
        offset=result.offset,
        truncated=result.truncated,
    )
    if result.exit_code is not None:
        message.exit_code = result.exit_code
    return message


def job_result_from_protobuf(message: pb.JobResult) -> JobResult:
    """Decode a protobuf job result into the canonical DTO."""

    return JobResult(
        job_id=message.job_id,
        done=message.done,
        status=job_status_from_protobuf(message.status),
        exit_code=_optional_int32_field(message, "exit_code"),
        output_path=message.output_path,
        output=message.output,
        offset=message.offset,
        truncated=message.truncated,
    )


def job_status_view_to_protobuf(view: JobStatusView) -> pb.JobStatusView:
    """Encode a materialized job status view for gRPC responses."""

    message = pb.JobStatusView(
        job_id=view.job_id,
        status=job_status_to_protobuf(view.status),
        done=view.done,
        created_at=view.created_at,
        offset=view.offset,
    )
    if view.exit_code is not None:
        message.exit_code = view.exit_code
    if view.started_at is not None:
        message.started_at = view.started_at
    if view.ended_at is not None:
        message.ended_at = view.ended_at
    return message


def job_status_view_from_protobuf(message: pb.JobStatusView) -> JobStatusView:
    """Decode a protobuf job status view into the canonical DTO."""

    return JobStatusView(
        job_id=message.job_id,
        status=job_status_from_protobuf(message.status),
        done=message.done,
        exit_code=_optional_int32_field(message, "exit_code"),
        created_at=message.created_at,
        started_at=_optional_string_field(message, "started_at"),
        ended_at=_optional_string_field(message, "ended_at"),
        offset=message.offset,
    )


def job_info_to_protobuf(info: JobInfo) -> pb.JobInfo:
    """Encode a compact job listing record for gRPC responses."""

    message = pb.JobInfo(
        job_id=info.job_id,
        status=job_status_to_protobuf(info.status),
        created_at=info.created_at,
    )
    if info.started_at is not None:
        message.started_at = info.started_at
    if info.ended_at is not None:
        message.ended_at = info.ended_at
    return message


def job_info_from_protobuf(message: pb.JobInfo) -> JobInfo:
    """Decode a protobuf job listing record into the canonical DTO."""

    return JobInfo(
        job_id=message.job_id,
        status=job_status_from_protobuf(message.status),
        created_at=message.created_at,
        started_at=_optional_string_field(message, "started_at"),
        ended_at=_optional_string_field(message, "ended_at"),
    )


def list_jobs_response_to_protobuf(response: ListJobsResponse) -> pb.ListJobsResponse:
    """Encode a job list response for gRPC transport."""

    message = pb.ListJobsResponse()
    message.jobs.extend([job_info_to_protobuf(job) for job in response.jobs])
    return message


def list_jobs_response_from_protobuf(message: pb.ListJobsResponse) -> ListJobsResponse:
    """Decode a protobuf job list response into the canonical DTO."""

    return ListJobsResponse(jobs=[job_info_from_protobuf(job) for job in message.jobs])


def delete_job_response_to_protobuf(
    response: DeleteJobResponse,
) -> pb.DeleteJobResponse:
    """Encode a delete response for gRPC transport."""

    return pb.DeleteJobResponse(job_id=response.job_id, deleted=response.deleted)


def delete_job_response_from_protobuf(
    message: pb.DeleteJobResponse,
) -> DeleteJobResponse:
    """Decode a protobuf delete response into the canonical DTO."""

    return DeleteJobResponse(job_id=message.job_id, deleted=message.deleted)


def health_response_to_protobuf(response: HealthResponse) -> pb.HealthResponse:
    """Encode a health response for gRPC transport."""

    return pb.HealthResponse(status=response.status)


def health_response_from_protobuf(message: pb.HealthResponse) -> HealthResponse:
    """Decode a protobuf health response into the canonical DTO."""

    return HealthResponse(status=message.status)


def _optional_string_field(message: Message, field_name: str) -> str | None:
    return getattr(message, field_name) if message.HasField(field_name) else None


def _optional_int32_field(message: Message, field_name: str) -> int | None:
    return int(getattr(message, field_name)) if message.HasField(field_name) else None


def _optional_uint32_field(message: Message, field_name: str, default: int) -> int:
    return (
        int(getattr(message, field_name)) if message.HasField(field_name) else default
    )


def _optional_float_field(message: Message, field_name: str, default: float) -> float:
    return (
        float(getattr(message, field_name)) if message.HasField(field_name) else default
    )


def _validate_list_limit(limit: int) -> int:
    if not 1 <= limit <= MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIST_LIMIT}")
    return limit


def _validate_output_limit(output_limit: int) -> int:
    if not 1 <= output_limit <= MAX_OUTPUT_LIMIT_BYTES:
        raise ValueError(f"output_limit must be between 1 and {MAX_OUTPUT_LIMIT_BYTES}")
    return output_limit


__all__ = [
    "delete_job_request_from_protobuf",
    "delete_job_request_to_protobuf",
    "delete_job_response_from_protobuf",
    "delete_job_response_to_protobuf",
    "get_job_status_request_from_protobuf",
    "get_job_status_request_to_protobuf",
    "health_response_from_protobuf",
    "health_response_to_protobuf",
    "input_job_request_from_protobuf",
    "input_job_request_to_protobuf",
    "job_info_from_protobuf",
    "job_info_to_protobuf",
    "job_result_from_protobuf",
    "job_result_to_protobuf",
    "job_status_from_protobuf",
    "job_status_to_protobuf",
    "job_status_view_from_protobuf",
    "job_status_view_to_protobuf",
    "list_jobs_request_from_protobuf",
    "list_jobs_request_to_protobuf",
    "list_jobs_response_from_protobuf",
    "list_jobs_response_to_protobuf",
    "optional_job_status_from_protobuf",
    "run_job_request_from_protobuf",
    "run_job_request_to_protobuf",
    "tail_job_request_from_protobuf",
    "tail_job_request_to_protobuf",
    "terminal_size_from_protobuf",
    "terminal_size_to_protobuf",
    "terminate_job_request_from_protobuf",
    "terminate_job_request_to_protobuf",
    "wait_job_request_from_protobuf",
    "wait_job_request_to_protobuf",
]

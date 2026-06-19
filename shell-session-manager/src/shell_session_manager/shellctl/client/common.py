"""Shared client-side abstractions for shellctl transports.

`ShellctlClient` is the stable public façade. HTTP and gRPC transports share the
same default request knobs and structured error decoding through this module so
adding a second protocol does not fragment SDK behavior.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Protocol

from grpclib.exceptions import GRPCError

from shell_session_manager.shellctl.server.error_codec import ErrorCodec
from shell_session_manager.shellctl.shared import (
    DEFAULT_AUTH_TOKEN_ENV,
    DeleteJobResponse,
    HealthResponse,
    InputJobRequest,
    JobResult,
    JobStatusView,
    ListJobsResponse,
    RunJobRequest,
    TerminateJobRequest,
    WaitJobRequest,
)


class ShellctlClientError(RuntimeError):
    """Raised when a shellctl transport returns a structured request failure."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{code} ({status_code}): {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(slots=True, frozen=True)
class ShellctlClientDefaults:
    """Per-client transport defaults shared across SDK calls."""

    output_limit: int
    idle_flush_seconds: float
    token: str | None


class ShellctlTransportProtocol(Protocol):
    """Transport interface implemented by the HTTP and gRPC SDK backends."""

    async def close(self) -> None: ...

    async def healthz(self) -> HealthResponse: ...

    async def run(self, request: RunJobRequest) -> JobResult: ...

    async def wait(self, job_id: str, request: WaitJobRequest) -> JobResult: ...

    async def status(self, job_id: str) -> JobStatusView: ...

    async def list_jobs(
        self, *, status: str | None, limit: int
    ) -> ListJobsResponse: ...

    async def input(self, job_id: str, request: InputJobRequest) -> JobResult: ...

    async def tail(self, job_id: str, *, output_limit: int) -> JobResult: ...

    async def terminate(
        self, job_id: str, request: TerminateJobRequest
    ) -> JobStatusView: ...

    async def delete(
        self,
        job_id: str,
        *,
        force: bool,
        grace_seconds: float | None,
    ) -> DeleteJobResponse: ...


def resolve_auth_token(explicit: str | None) -> str | None:
    """Resolve the per-client bearer token using the legacy env fallback."""

    token = explicit if explicit is not None else os.environ.get(DEFAULT_AUTH_TOKEN_ENV)
    return token or None


def auth_header(token: str | None) -> dict[str, str]:
    """Build the auth header/metadata mapping for enabled bearer auth."""

    if not token:
        return {}
    return {"authorization": f"Bearer {token}"}


def decode_http_error(
    *,
    status_code: int,
    payload: object,
    fallback_message: str,
) -> ShellctlClientError:
    """Decode the shellctl HTTP error envelope into `ShellctlClientError`."""

    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code", "request_failed"))
        message = str(error.get("message", fallback_message))
    else:
        code = "request_failed"
        message = fallback_message
    return ShellctlClientError(status_code, code, message)


def decode_grpc_error(error: GRPCError) -> ShellctlClientError:
    """Decode a grpclib error into the SDK's stable public exception type."""

    if error.message:
        try:
            payload = json.loads(error.message)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            status_code = payload.get("status_code")
            code = payload.get("code")
            message = payload.get("message")
            if (
                isinstance(status_code, int)
                and isinstance(code, str)
                and isinstance(message, str)
            ):
                return ShellctlClientError(status_code, code, message)

    status_code = ErrorCodec().grpc_status_code(error.status)
    message = error.message or error.status.name.lower()
    return ShellctlClientError(status_code, "grpc_error", message)


__all__ = [
    "ShellctlClientDefaults",
    "ShellctlClientError",
    "ShellctlTransportProtocol",
    "auth_header",
    "decode_grpc_error",
    "decode_http_error",
    "resolve_auth_token",
]

"""Shared shellctl error translation for HTTP and gRPC transports.

The service raises `ShellctlServerError` as its structured domain failure type.
HTTP and gRPC both translate through this module so status codes, machine codes,
and user-facing messages stay aligned across transports.

For gRPC, shellctl intentionally serializes a compact JSON object into the
`GRPCError.message` field with the shape
`{"status_code": int, "code": str, "message": str}`. The client-side
`shell_session_manager.shellctl.client.common.decode_grpc_error()` function
consumes that payload to reconstruct the public `ShellctlClientError` shape.
"""

from __future__ import annotations

import json
from typing import ClassVar

from grpclib.const import Status
from grpclib.exceptions import GRPCError
from pydantic import ValidationError

from shell_session_manager.shellctl.server.errors import ShellctlServerError
from shell_session_manager.shellctl.shared import ErrorDetail, ErrorResponse


class ErrorCodec:
    """Map internal shellctl exceptions into transport-specific error payloads."""

    _GRPC_STATUS_BY_HTTP: ClassVar[dict[int, Status]] = {
        400: Status.INVALID_ARGUMENT,
        401: Status.UNAUTHENTICATED,
        404: Status.NOT_FOUND,
        409: Status.FAILED_PRECONDITION,
    }

    def normalize_exception(self, exc: Exception) -> ShellctlServerError:
        """Convert transport-bound exceptions into `ShellctlServerError`.

        Validation errors become `400 invalid_request` so the gRPC boundary keeps
        the same structured client error shape as HTTP business errors. Any other
        runtime exception is treated as an internal server error.
        """

        if isinstance(exc, ShellctlServerError):
            return exc
        if isinstance(exc, ValidationError):
            return ShellctlServerError(
                400,
                "invalid_request",
                self._validation_message(exc),
            )
        if isinstance(exc, (ValueError, TypeError)):
            return ShellctlServerError(
                400, "invalid_request", str(exc) or "invalid request"
            )
        if isinstance(exc, RuntimeError):
            return ShellctlServerError(
                500,
                "internal_error",
                str(exc) or "internal server error",
            )
        return ShellctlServerError(500, "internal_error", "internal server error")

    def to_error_response(self, exc: Exception) -> ErrorResponse:
        """Build the canonical HTTP-style error envelope for an exception."""

        normalized = self.normalize_exception(exc)
        return ErrorResponse(
            error=ErrorDetail(code=normalized.code, message=normalized.message)
        )

    def to_http_content(self, exc: Exception) -> dict[str, object]:
        """Render the canonical error envelope as JSON-serializable content."""

        return self.to_error_response(exc).model_dump(mode="json")

    def to_grpc_error(self, exc: Exception) -> GRPCError:
        """Translate an exception into a structured grpclib `GRPCError`.

        The returned gRPC status message is a compact JSON object containing
        `status_code`, `code`, and `message`. That wire contract is shared with
        `client.common.decode_grpc_error()` so gRPC callers see the same
        `ShellctlClientError` fields as HTTP callers.
        """

        normalized = self.normalize_exception(exc)
        status = self._GRPC_STATUS_BY_HTTP.get(normalized.status_code, Status.INTERNAL)
        payload = json.dumps(
            {
                "status_code": normalized.status_code,
                "code": normalized.code,
                "message": normalized.message,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return GRPCError(status, payload)

    def grpc_status_code(self, status: Status) -> int:
        """Best-effort reverse mapping when shellctl JSON details are absent.

        This is only a fallback for non-shellctl or corrupted gRPC errors where
        `decode_grpc_error()` cannot recover the canonical JSON payload from the
        status message.
        """

        for status_code, grpc_status in self._GRPC_STATUS_BY_HTTP.items():
            if grpc_status is status:
                return status_code
        return 500

    def _validation_message(self, exc: ValidationError) -> str:
        details: list[str] = []
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error.get("loc", ()))
            message = str(error.get("msg", "invalid request"))
            details.append(f"{location}: {message}" if location else message)
        return "; ".join(details) or str(exc) or "invalid request"


__all__ = ["ErrorCodec"]

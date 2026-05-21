"""Async HTTP client for the shellctl server API.

The SDK keeps transport-level knobs (`output_limit`, `idle_flush_seconds`, and
 bearer token handling) on the client instance so individual method calls stay
 close to the proposal's high-level workflow.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from shell_session_manager.shellctl.shared import (
    DEFAULT_AUTH_TOKEN_ENV,
    DEFAULT_IDLE_FLUSH_SECONDS,
    DEFAULT_LIST_LIMIT,
    DEFAULT_OUTPUT_LIMIT_BYTES,
    DEFAULT_TERMINATE_GRACE_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
    DeleteJobResponse,
    JobInfo,
    JobResult,
    JobStatusView,
    ListJobsResponse,
    RunJobRequest,
    TerminalSize,
)


class ShellctlClientError(RuntimeError):
    """Raised when the shellctl server returns an error response."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{code} ({status_code}): {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


class ShellctlClient:
    """Thin async SDK for the shellctl HTTP API.

    The client owns a reusable `httpx.AsyncClient` unless one is injected via the
    `client` argument. Callers can therefore either keep one instance for a full
    workflow or treat it as an async context manager.
    """

    def __init__(
        self,
        base_url: str,
        *,
        output_limit: int = DEFAULT_OUTPUT_LIMIT_BYTES,
        idle_flush_seconds: float = DEFAULT_IDLE_FLUSH_SECONDS,
        token: str | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.output_limit = output_limit
        self.idle_flush_seconds = idle_flush_seconds
        self.token = (
            token if token is not None else os.environ.get(DEFAULT_AUTH_TOKEN_ENV)
        )
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=self.base_url,
            follow_redirects=True,
            timeout=httpx.Timeout(
                DEFAULT_TIMEOUT_SECONDS, connect=DEFAULT_TIMEOUT_SECONDS
            ),
            transport=transport,
        )

    async def __aenter__(self) -> ShellctlClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying HTTP client if this SDK instance owns it."""

        if self._owns_client:
            await self._client.aclose()

    async def healthz(self) -> dict[str, Any]:
        """Call the public health endpoint without requiring auth."""

        response = await self._client.get("/healthz")
        return self._decode_response(response)

    async def run(
        self,
        script: str,
        *,
        cwd: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        terminal: TerminalSize | None = None,
    ) -> JobResult:
        """Create a new job and wait for initial output or completion."""

        payload = RunJobRequest(
            script=script,
            cwd=cwd,
            terminal=terminal,
            timeout=timeout,
            output_limit=self.output_limit,
            idle_flush_seconds=self.idle_flush_seconds,
        )
        response = await self._client.post(
            "/v1/jobs/run",
            json=payload.model_dump(mode="json", exclude_none=True),
            headers=self._auth_headers(),
        )
        return JobResult.model_validate(self._decode_response(response))

    async def wait(
        self,
        job_id: str,
        *,
        offset: int,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> JobResult:
        """Wait for incremental output, completion, truncation, or timeout."""

        response = await self._client.post(
            f"/v1/jobs/{job_id}/wait",
            json={
                "offset": offset,
                "timeout": timeout,
                "output_limit": self.output_limit,
                "idle_flush_seconds": self.idle_flush_seconds,
            },
            headers=self._auth_headers(),
        )
        return JobResult.model_validate(self._decode_response(response))

    async def status(self, job_id: str) -> JobStatusView:
        """Fetch the materialized status view for one job."""

        response = await self._client.get(
            f"/v1/jobs/{job_id}",
            headers=self._auth_headers(),
        )
        return JobStatusView.model_validate(self._decode_response(response))

    async def list_jobs(
        self,
        *,
        status: str | None = None,
        limit: int = DEFAULT_LIST_LIMIT,
    ) -> list[JobInfo]:
        """List recent jobs, optionally filtered by lifecycle status."""

        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        response = await self._client.get(
            "/v1/jobs",
            params=params,
            headers=self._auth_headers(),
        )
        payload = ListJobsResponse.model_validate(self._decode_response(response))
        return payload.jobs

    async def input(
        self,
        job_id: str,
        text: str,
        *,
        offset: int,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> JobResult:
        """Send text input to a running job and then wait like `wait()`."""

        response = await self._client.post(
            f"/v1/jobs/{job_id}/input",
            json={
                "text": text,
                "offset": offset,
                "timeout": timeout,
                "output_limit": self.output_limit,
                "idle_flush_seconds": self.idle_flush_seconds,
            },
            headers=self._auth_headers(),
        )
        return JobResult.model_validate(self._decode_response(response))

    async def tail(self, job_id: str) -> JobResult:
        """Fetch an immediate UTF-8-safe tail snapshot for a job."""

        response = await self._client.get(
            f"/v1/jobs/{job_id}/log/tail",
            params={"output_limit": self.output_limit},
            headers=self._auth_headers(),
        )
        return JobResult.model_validate(self._decode_response(response))

    async def terminate(
        self,
        job_id: str,
        grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
    ) -> JobStatusView:
        """Terminate a job, returning the resulting materialized status view."""

        response = await self._client.post(
            f"/v1/jobs/{job_id}/terminate",
            json={"grace_seconds": grace_seconds},
            headers=self._auth_headers(),
        )
        return JobStatusView.model_validate(self._decode_response(response))

    async def delete(
        self,
        job_id: str,
        *,
        force: bool = False,
        grace_seconds: float | None = None,
    ) -> DeleteJobResponse:
        """Delete job artifacts, optionally terminating the job first."""

        params: dict[str, Any] = {"force": str(force).lower()}
        if grace_seconds is not None:
            params["grace_seconds"] = grace_seconds
        response = await self._client.delete(
            f"/v1/jobs/{job_id}",
            params=params,
            headers=self._auth_headers(),
        )
        return DeleteJobResponse.model_validate(self._decode_response(response))

    def _auth_headers(self) -> dict[str, str]:
        if not self.token:
            return {}
        return {"Authorization": f"Bearer {self.token}"}

    def _decode_response(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - network/proxy corruption
            raise ShellctlClientError(
                response.status_code, "invalid_json", response.text
            ) from exc

        if response.is_error:
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                code = str(error.get("code", "request_failed"))
                message = str(error.get("message", response.text))
            else:
                code = "request_failed"
                message = response.text
            raise ShellctlClientError(response.status_code, code, message)

        if not isinstance(payload, dict):
            raise ShellctlClientError(
                response.status_code, "invalid_payload", response.text
            )
        return payload


__all__ = ["ShellctlClient", "ShellctlClientError"]

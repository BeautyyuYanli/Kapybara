"""HTTP transport implementation for the shellctl SDK façade."""

from __future__ import annotations

from typing import Any

import httpx

from shell_session_manager.shellctl.client.common import (
    ShellctlClientDefaults,
    ShellctlClientError,
    auth_header,
    decode_http_error,
)
from shell_session_manager.shellctl.shared import (
    DEFAULT_TIMEOUT_SECONDS,
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


class HttpShellctlTransport:
    """httpx-backed shellctl transport used by `http://` and `https://` endpoints.

    The transport preserves the existing ability to inject either an
    `httpx.AsyncClient` or a low-level httpx transport for tests.
    """

    def __init__(
        self,
        base_url: str,
        *,
        defaults: ShellctlClientDefaults,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._defaults = defaults
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            follow_redirects=True,
            timeout=httpx.Timeout(
                DEFAULT_TIMEOUT_SECONDS, connect=DEFAULT_TIMEOUT_SECONDS
            ),
            transport=transport,
        )

    async def close(self) -> None:
        """Close the owned httpx client, if any."""

        if self._owns_client:
            await self._client.aclose()

    async def healthz(self) -> HealthResponse:
        """Call the public shellctl health endpoint."""

        response = await self._client.get("/healthz")
        return HealthResponse.model_validate(self._decode_payload(response))

    async def run(self, request: RunJobRequest) -> JobResult:
        """POST `/v1/jobs/run`."""

        response = await self._client.post(
            "/v1/jobs/run",
            json=request.model_dump(mode="json", exclude_none=True),
            headers=self._auth_headers(),
        )
        return JobResult.model_validate(self._decode_payload(response))

    async def wait(self, job_id: str, request: WaitJobRequest) -> JobResult:
        """POST `/v1/jobs/{job_id}/wait`."""

        response = await self._client.post(
            f"/v1/jobs/{job_id}/wait",
            json=request.model_dump(mode="json"),
            headers=self._auth_headers(),
        )
        return JobResult.model_validate(self._decode_payload(response))

    async def status(self, job_id: str) -> JobStatusView:
        """GET `/v1/jobs/{job_id}`."""

        response = await self._client.get(
            f"/v1/jobs/{job_id}",
            headers=self._auth_headers(),
        )
        return JobStatusView.model_validate(self._decode_payload(response))

    async def list_jobs(self, *, status: str | None, limit: int) -> ListJobsResponse:
        """GET `/v1/jobs`."""

        params: dict[str, Any] = {"limit": limit}
        if status is not None:
            params["status"] = status
        response = await self._client.get(
            "/v1/jobs",
            params=params,
            headers=self._auth_headers(),
        )
        return ListJobsResponse.model_validate(self._decode_payload(response))

    async def input(self, job_id: str, request: InputJobRequest) -> JobResult:
        """POST `/v1/jobs/{job_id}/input`."""

        response = await self._client.post(
            f"/v1/jobs/{job_id}/input",
            json=request.model_dump(mode="json"),
            headers=self._auth_headers(),
        )
        return JobResult.model_validate(self._decode_payload(response))

    async def tail(self, job_id: str, *, output_limit: int) -> JobResult:
        """GET `/v1/jobs/{job_id}/log/tail`."""

        response = await self._client.get(
            f"/v1/jobs/{job_id}/log/tail",
            params={"output_limit": output_limit},
            headers=self._auth_headers(),
        )
        return JobResult.model_validate(self._decode_payload(response))

    async def terminate(
        self, job_id: str, request: TerminateJobRequest
    ) -> JobStatusView:
        """POST `/v1/jobs/{job_id}/terminate`."""

        response = await self._client.post(
            f"/v1/jobs/{job_id}/terminate",
            json=request.model_dump(mode="json"),
            headers=self._auth_headers(),
        )
        return JobStatusView.model_validate(self._decode_payload(response))

    async def delete(
        self,
        job_id: str,
        *,
        force: bool,
        grace_seconds: float | None,
    ) -> DeleteJobResponse:
        """DELETE `/v1/jobs/{job_id}`."""

        params: dict[str, Any] = {"force": str(force).lower()}
        if grace_seconds is not None:
            params["grace_seconds"] = grace_seconds
        response = await self._client.delete(
            f"/v1/jobs/{job_id}",
            params=params,
            headers=self._auth_headers(),
        )
        return DeleteJobResponse.model_validate(self._decode_payload(response))

    def _auth_headers(self) -> dict[str, str]:
        return auth_header(self._defaults.token)

    def _decode_payload(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:  # pragma: no cover - network/proxy corruption
            raise ShellctlClientError(
                response.status_code, "invalid_json", response.text
            ) from exc

        if response.is_error:
            raise decode_http_error(
                status_code=response.status_code,
                payload=payload,
                fallback_message=response.text,
            )
        if not isinstance(payload, dict):
            raise ShellctlClientError(
                response.status_code,
                "invalid_payload",
                response.text,
            )
        return payload


__all__ = ["HttpShellctlTransport"]

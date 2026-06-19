"""Protocol-neutral endpoint delegation for shellctl server transports.

`ShellctlService` remains the business lifecycle source of truth. HTTP routes and
gRPC handlers should only unpack protocol-specific input, call this controller,
and re-encode the result.
"""

from __future__ import annotations

from shell_session_manager.shellctl.server.service import ShellctlService
from shell_session_manager.shellctl.shared import (
    DEFAULT_HEALTH_STATUS,
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


class ShellctlEndpointController:
    """Thin transport-agnostic façade over `ShellctlService` methods.

    The controller intentionally contains no independent lifecycle state so HTTP
    and gRPC continue to converge through one service implementation.
    """

    def __init__(self, service: ShellctlService) -> None:
        self._service = service

    async def health(self) -> HealthResponse:
        """Return the public health payload without touching service state."""

        return HealthResponse(status=DEFAULT_HEALTH_STATUS)

    async def run_job(self, request: RunJobRequest) -> JobResult:
        """Delegate job creation to `ShellctlService`."""

        return await self._service.run_job(request)

    async def wait_job(self, job_id: str, request: WaitJobRequest) -> JobResult:
        """Delegate long-poll wait semantics to `ShellctlService`."""

        return await self._service.wait_job(job_id, request)

    async def tail_job(self, job_id: str, *, output_limit: int) -> JobResult:
        """Delegate immediate output tail reads to `ShellctlService`."""

        return await self._service.tail_job(job_id, output_limit=output_limit)

    async def get_job_status(self, job_id: str) -> JobStatusView:
        """Delegate materialized job status reads to `ShellctlService`."""

        return await self._service.get_job_status(job_id)

    async def list_jobs(
        self,
        *,
        status: JobStatusName | None,
        limit: int,
    ) -> ListJobsResponse:
        """Delegate recent job listing to `ShellctlService`."""

        return await self._service.list_jobs(status=status, limit=limit)

    async def send_input(self, job_id: str, request: InputJobRequest) -> JobResult:
        """Delegate interactive input handling to `ShellctlService`."""

        return await self._service.send_input(job_id, request)

    async def terminate_job(
        self, job_id: str, request: TerminateJobRequest
    ) -> JobStatusView:
        """Delegate termination semantics to `ShellctlService`."""

        return await self._service.terminate_job(job_id, request)

    async def delete_job(
        self,
        job_id: str,
        *,
        force: bool,
        grace_seconds: float,
    ) -> DeleteJobResponse:
        """Delegate deletion semantics to `ShellctlService`."""

        return await self._service.delete_job(
            job_id,
            force=force,
            grace_seconds=grace_seconds,
        )


__all__ = ["ShellctlEndpointController"]

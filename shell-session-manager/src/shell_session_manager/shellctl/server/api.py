"""FastAPI wiring for the shellctl HTTP transport.

This module keeps HTTP-specific concerns such as route registration and FastAPI
lifespan management. Auth checks, error encoding, and endpoint delegation are
shared with the gRPC transport through dedicated helper modules.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse

from shell_session_manager.shellctl.server.auth import AuthVerifier
from shell_session_manager.shellctl.server.config import ShellctlConfig
from shell_session_manager.shellctl.server.error_codec import ErrorCodec
from shell_session_manager.shellctl.server.errors import ShellctlServerError
from shell_session_manager.shellctl.server.service import ShellctlService
from shell_session_manager.shellctl.server.transport import ShellctlEndpointController
from shell_session_manager.shellctl.shared import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_TERMINATE_GRACE_SECONDS,
    MAX_LIST_LIMIT,
    MAX_OUTPUT_LIMIT_BYTES,
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


def create_app(
    config: ShellctlConfig | None = None,
    *,
    service: ShellctlService | None = None,
) -> FastAPI:
    """Create the FastAPI application used by `shellctl serve`."""

    resolved_config = config or ShellctlConfig()
    resolved_service = service or ShellctlService(resolved_config)
    controller = ShellctlEndpointController(resolved_service)
    auth_verifier = AuthVerifier.from_config(resolved_config)
    error_codec = ErrorCodec()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await resolved_service.initialize()
        resolved_service.start_background_gc()
        resolved_service.start_background_pipe_monitor()
        try:
            yield
        finally:
            await resolved_service.shutdown()

    app = FastAPI(title="shellctl", version="0.1.0", lifespan=lifespan)
    app.state.shellctl_service = resolved_service

    @app.exception_handler(ShellctlServerError)
    async def handle_shellctl_error(
        _request: Request,
        exc: ShellctlServerError,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=error_codec.to_http_content(exc),
        )

    @app.exception_handler(RuntimeError)
    async def handle_runtime_error(
        _request: Request, exc: RuntimeError
    ) -> JSONResponse:
        normalized = error_codec.normalize_exception(exc)
        return JSONResponse(
            status_code=normalized.status_code,
            content=error_codec.to_http_content(normalized),
        )

    def verify_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        auth_verifier.verify_authorization_header(authorization)

    @app.get("/healthz", response_model=HealthResponse)
    async def healthz() -> HealthResponse:
        return await controller.health()

    @app.post(
        "/v1/jobs/run",
        response_model=JobResult,
        dependencies=[Depends(verify_auth)],
    )
    async def run_job(
        payload: RunJobRequest,
    ) -> JobResult:
        return await controller.run_job(payload)

    @app.post(
        "/v1/jobs/{job_id}/wait",
        response_model=JobResult,
        dependencies=[Depends(verify_auth)],
    )
    async def wait_job(
        job_id: str,
        payload: WaitJobRequest,
    ) -> JobResult:
        return await controller.wait_job(job_id, payload)

    @app.get(
        "/v1/jobs/{job_id}/log/tail",
        response_model=JobResult,
        dependencies=[Depends(verify_auth)],
    )
    async def tail_job(
        job_id: str,
        output_limit: Annotated[
            int, Query(ge=1, le=MAX_OUTPUT_LIMIT_BYTES)
        ] = resolved_config.default_output_limit_bytes,
    ) -> JobResult:
        return await controller.tail_job(job_id, output_limit=output_limit)

    @app.get(
        "/v1/jobs/{job_id}",
        response_model=JobStatusView,
        dependencies=[Depends(verify_auth)],
    )
    async def job_status(
        job_id: str,
    ) -> JobStatusView:
        return await controller.get_job_status(job_id)

    @app.get(
        "/v1/jobs",
        response_model=ListJobsResponse,
        dependencies=[Depends(verify_auth)],
    )
    async def list_jobs(
        status: Annotated[JobStatusName | None, Query()] = None,
        limit: Annotated[int, Query(ge=1, le=MAX_LIST_LIMIT)] = DEFAULT_LIST_LIMIT,
    ) -> ListJobsResponse:
        return await controller.list_jobs(status=status, limit=limit)

    @app.post(
        "/v1/jobs/{job_id}/input",
        response_model=JobResult,
        dependencies=[Depends(verify_auth)],
    )
    async def input_job(
        job_id: str,
        payload: InputJobRequest,
    ) -> JobResult:
        return await controller.send_input(job_id, payload)

    @app.post(
        "/v1/jobs/{job_id}/terminate",
        response_model=JobStatusView,
        dependencies=[Depends(verify_auth)],
    )
    async def terminate_job(
        job_id: str,
        payload: TerminateJobRequest,
    ) -> JobStatusView:
        return await controller.terminate_job(job_id, payload)

    @app.delete(
        "/v1/jobs/{job_id}",
        response_model=DeleteJobResponse,
        dependencies=[Depends(verify_auth)],
    )
    async def delete_job(
        job_id: str,
        force: bool = False,
        grace_seconds: float = DEFAULT_TERMINATE_GRACE_SECONDS,
    ) -> DeleteJobResponse:
        return await controller.delete_job(
            job_id,
            force=force,
            grace_seconds=grace_seconds,
        )

    return app


__all__ = ["create_app"]

"""Direct shellctl CLI controller for one-shot job management commands.

This module mirrors the HTTP transport contract while calling
`ShellctlService` directly. Each command creates a fresh service instance,
uses `prepare_runtime()` for the minimal local runtime bootstrap, runs one job
operation, emits compact JSON on stdout, and then shuts the service down.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import NoReturn

import anyio
import typer
from pydantic import BaseModel, ValidationError

from shell_session_manager.shellctl.server.config import ShellctlConfig
from shell_session_manager.shellctl.server.errors import ShellctlServerError
from shell_session_manager.shellctl.server.service import ShellctlService
from shell_session_manager.shellctl.shared import (
    DEFAULT_HEALTH_STATUS,
    DEFAULT_IDLE_FLUSH_SECONDS,
    DEFAULT_LIST_LIMIT,
    DEFAULT_OUTPUT_LIMIT_BYTES,
    DEFAULT_TERMINATE_GRACE_SECONDS,
    DEFAULT_TIMEOUT_SECONDS,
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
    TerminalSize,
    TerminateJobRequest,
    WaitJobRequest,
    default_state_dir,
)


def register_cli_controller(root: typer.Typer) -> None:
    """Register direct local job-management commands on the root CLI."""

    root.command("health")(health_command)
    root.command("run")(run_command)
    root.command("wait")(wait_command)
    root.command("status")(status_command)
    root.command("list")(list_command)
    root.command("input")(input_command)
    root.command("tail")(tail_command)
    root.command("terminate")(terminate_command)
    root.command("delete")(delete_command)


def health_command(
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    runtime_dir: Path | None = typer.Option(None, "--runtime-dir"),
) -> None:
    """Prepare the local shellctl runtime and report health as JSON."""

    config = _build_config(state_dir=state_dir, runtime_dir=runtime_dir)

    async def action(_service: ShellctlService) -> HealthResponse:
        return HealthResponse(status=DEFAULT_HEALTH_STATUS)

    _run_action(config, action)


def run_command(
    script: str = typer.Argument(...),
    cwd: Path | None = typer.Option(None, "--cwd"),
    env: list[str] | None = typer.Option(None, "--env"),
    timeout: float = typer.Option(DEFAULT_TIMEOUT_SECONDS, "--timeout"),
    output_limit: int = typer.Option(DEFAULT_OUTPUT_LIMIT_BYTES, "--output-limit"),
    idle_flush_seconds: float = typer.Option(
        DEFAULT_IDLE_FLUSH_SECONDS,
        "--idle-flush-seconds",
    ),
    cols: int | None = typer.Option(None, "--cols"),
    rows: int | None = typer.Option(None, "--rows"),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    runtime_dir: Path | None = typer.Option(None, "--runtime-dir"),
) -> None:
    """Create a tmux-backed job and wait for its initial result window."""

    config = _build_config(state_dir=state_dir, runtime_dir=runtime_dir)
    request = _build_model(
        RunJobRequest,
        script=script,
        cwd=str(cwd) if cwd is not None else None,
        env=_parse_env(env),
        terminal=_terminal_size(cols=cols, rows=rows, config=config),
        timeout=timeout,
        output_limit=output_limit,
        idle_flush_seconds=idle_flush_seconds,
    )

    async def action(service: ShellctlService) -> JobResult:
        return await service.run_job(request)

    _run_action(config, action)


def wait_command(
    job_id: str = typer.Argument(...),
    offset: int = typer.Option(..., "--offset"),
    timeout: float = typer.Option(DEFAULT_TIMEOUT_SECONDS, "--timeout"),
    output_limit: int = typer.Option(DEFAULT_OUTPUT_LIMIT_BYTES, "--output-limit"),
    idle_flush_seconds: float = typer.Option(
        DEFAULT_IDLE_FLUSH_SECONDS,
        "--idle-flush-seconds",
    ),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    runtime_dir: Path | None = typer.Option(None, "--runtime-dir"),
) -> None:
    """Wait for job output, completion, truncation, or timeout."""

    config = _build_config(state_dir=state_dir, runtime_dir=runtime_dir)
    request = _build_model(
        WaitJobRequest,
        offset=offset,
        timeout=timeout,
        output_limit=output_limit,
        idle_flush_seconds=idle_flush_seconds,
    )

    async def action(service: ShellctlService) -> JobResult:
        return await service.wait_job(job_id, request)

    _run_action(config, action)


def status_command(
    job_id: str = typer.Argument(...),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    runtime_dir: Path | None = typer.Option(None, "--runtime-dir"),
) -> None:
    """Materialize the current status view for one job."""

    config = _build_config(state_dir=state_dir, runtime_dir=runtime_dir)

    async def action(service: ShellctlService) -> JobStatusView:
        return await service.get_job_status(job_id)

    _run_action(config, action)


def list_command(
    status: JobStatusName | None = typer.Option(None, "--status"),
    limit: int = typer.Option(DEFAULT_LIST_LIMIT, "--limit", min=1, max=MAX_LIST_LIMIT),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    runtime_dir: Path | None = typer.Option(None, "--runtime-dir"),
) -> None:
    """List recent jobs, optionally filtered by status."""

    config = _build_config(state_dir=state_dir, runtime_dir=runtime_dir)

    async def action(service: ShellctlService) -> ListJobsResponse:
        return await service.list_jobs(status=status, limit=limit)

    _run_action(config, action)


def input_command(
    job_id: str = typer.Argument(...),
    text: str = typer.Argument(...),
    offset: int = typer.Option(..., "--offset"),
    timeout: float = typer.Option(DEFAULT_TIMEOUT_SECONDS, "--timeout"),
    output_limit: int = typer.Option(DEFAULT_OUTPUT_LIMIT_BYTES, "--output-limit"),
    idle_flush_seconds: float = typer.Option(
        DEFAULT_IDLE_FLUSH_SECONDS,
        "--idle-flush-seconds",
    ),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    runtime_dir: Path | None = typer.Option(None, "--runtime-dir"),
) -> None:
    """Send text input to a running job and wait for the next result window."""

    config = _build_config(state_dir=state_dir, runtime_dir=runtime_dir)
    request = _build_model(
        InputJobRequest,
        text=text,
        offset=offset,
        timeout=timeout,
        output_limit=output_limit,
        idle_flush_seconds=idle_flush_seconds,
    )

    async def action(service: ShellctlService) -> JobResult:
        return await service.send_input(job_id, request)

    _run_action(config, action)


def tail_command(
    job_id: str = typer.Argument(...),
    output_limit: int = typer.Option(
        DEFAULT_OUTPUT_LIMIT_BYTES,
        "--output-limit",
        min=1,
        max=MAX_OUTPUT_LIMIT_BYTES,
    ),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    runtime_dir: Path | None = typer.Option(None, "--runtime-dir"),
) -> None:
    """Read a UTF-8-safe output tail for one job."""

    config = _build_config(state_dir=state_dir, runtime_dir=runtime_dir)

    async def action(service: ShellctlService) -> JobResult:
        return await service.tail_job(job_id, output_limit=output_limit)

    _run_action(config, action)


def terminate_command(
    job_id: str = typer.Argument(...),
    grace_seconds: float = typer.Option(
        DEFAULT_TERMINATE_GRACE_SECONDS,
        "--grace-seconds",
    ),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    runtime_dir: Path | None = typer.Option(None, "--runtime-dir"),
) -> None:
    """Terminate a job and return its materialized status."""

    config = _build_config(state_dir=state_dir, runtime_dir=runtime_dir)
    request = _build_model(TerminateJobRequest, grace_seconds=grace_seconds)

    async def action(service: ShellctlService) -> JobStatusView:
        return await service.terminate_job(job_id, request)

    _run_action(config, action)


def delete_command(
    job_id: str = typer.Argument(...),
    force: bool = typer.Option(False, "--force"),
    grace_seconds: float = typer.Option(
        DEFAULT_TERMINATE_GRACE_SECONDS,
        "--grace-seconds",
    ),
    state_dir: Path | None = typer.Option(None, "--state-dir"),
    runtime_dir: Path | None = typer.Option(None, "--runtime-dir"),
) -> None:
    """Delete a job row and artifacts, optionally terminating first."""

    config = _build_config(state_dir=state_dir, runtime_dir=runtime_dir)
    request = _build_model(TerminateJobRequest, grace_seconds=grace_seconds)

    async def action(service: ShellctlService) -> DeleteJobResponse:
        return await service.delete_job(
            job_id,
            force=force,
            grace_seconds=request.grace_seconds,
        )

    _run_action(config, action)


def _build_config(
    *,
    state_dir: Path | None,
    runtime_dir: Path | None,
) -> ShellctlConfig:
    """Build config for direct local commands without HTTP auth env fallback.

    Passing `auth_token=""` intentionally lets `ShellctlConfig.__post_init__`
    normalize the token to `None` while preventing the direct CLI path from
    inheriting `SHELLCTL_AUTH_TOKEN`, which is only meaningful for `serve`.
    """

    return ShellctlConfig(
        auth_token="",
        state_dir=state_dir or default_state_dir(),
        runtime_dir=runtime_dir,
    )


def _parse_env(values: list[str] | None) -> dict[str, str] | None:
    if not values:
        return None

    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise typer.BadParameter(
                "env entries must use NAME=VALUE format",
                param_hint="--env",
            )
        name, env_value = value.split("=", 1)
        if not name:
            raise typer.BadParameter(
                "env names must be non-empty",
                param_hint="--env",
            )
        parsed[name] = env_value
    return parsed


def _terminal_size(
    *,
    cols: int | None,
    rows: int | None,
    config: ShellctlConfig,
) -> TerminalSize | None:
    if cols is None and rows is None:
        return None
    return _build_model(
        TerminalSize,
        cols=cols if cols is not None else config.default_terminal_cols,
        rows=rows if rows is not None else config.default_terminal_rows,
    )


def _build_model[ModelT: BaseModel](
    model_type: type[ModelT], /, **data: object
) -> ModelT:
    try:
        return model_type(**data)
    except ValidationError as exc:
        raise typer.BadParameter(_validation_error_message(exc)) from exc


async def _with_service[ResponseT: BaseModel](
    config: ShellctlConfig,
    action: Callable[[ShellctlService], Awaitable[ResponseT]],
) -> ResponseT:
    service = ShellctlService(config)
    await service.prepare_runtime()
    try:
        return await action(service)
    finally:
        await service.shutdown()


def _run_action[ResponseT: BaseModel](
    config: ShellctlConfig,
    action: Callable[[ShellctlService], Awaitable[ResponseT]],
) -> None:
    try:
        model = anyio.run(_with_service, config, action)
    except ShellctlServerError as exc:
        _handle_server_error(exc)
    _emit_model(model)


def _emit_model(model: BaseModel) -> None:
    typer.echo(model.model_dump_json(exclude_none=True), color=False)


def _handle_server_error(exc: ShellctlServerError) -> NoReturn:
    typer.echo(f"{exc.code}: {exc.message}", err=True, color=False)
    raise typer.Exit(code=1)


def _validation_error_message(exc: ValidationError) -> str:
    detail = exc.errors(include_url=False)[0]
    location = ".".join(str(part) for part in detail.get("loc", ()))
    message = detail["msg"]
    return f"{location}: {message}" if location else str(message)


__all__ = ["register_cli_controller"]

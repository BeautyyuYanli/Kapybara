"""Typer CLI entrypoints for the shellctl server package.

The server CLI only exposes commands that need the FastAPI/SQLite runtime.
The PTY sanitizer now lives in `shell_session_manager.shellctl.sanitize_pty`
as a separate lightweight module so tmux pipes do not have a second, heavier
entry path to maintain.
"""

from __future__ import annotations

from pathlib import Path

import anyio
import typer
import uvicorn

from shell_session_manager.shellctl.server.api import create_app
from shell_session_manager.shellctl.server.config import ShellctlConfig
from shell_session_manager.shellctl.server.service import ShellctlService
from shell_session_manager.shellctl.shared import (
    DEFAULT_AUTH_TOKEN_ENV,
    DEFAULT_GC_FINISHED_JOB_RETENTION_SECONDS,
    DEFAULT_GC_INTERVAL_SECONDS,
    default_state_dir,
)

cli = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


@cli.command("serve")
def serve_command(
    listen: str = "127.0.0.1:8765",
    auth_token: str | None = typer.Option(
        None,
        "--auth-token",
        envvar=DEFAULT_AUTH_TOKEN_ENV,
        help=(
            "Bearer token value. You can also set SHELLCTL_AUTH_TOKEN. "
            "Leave it unset or empty to disable HTTP bearer auth."
        ),
    ),
    state_dir: Path | None = None,
    runtime_dir: Path | None = None,
    gc_interval_seconds: float = DEFAULT_GC_INTERVAL_SECONDS,
    gc_finished_job_retention_seconds: float = DEFAULT_GC_FINISHED_JOB_RETENTION_SECONDS,
) -> None:
    """Run the shellctl FastAPI server via uvicorn."""

    host, port = _parse_listen(listen)
    config = ShellctlConfig(
        listen=listen,
        auth_token=auth_token,
        state_dir=state_dir or default_state_dir(),
        runtime_dir=runtime_dir,
        gc_interval_seconds=gc_interval_seconds,
        gc_finished_job_retention_seconds=gc_finished_job_retention_seconds,
    )
    uvicorn.run(create_app(config), host=host, port=port, log_level="info")


@cli.command("runner-exit")
def runner_exit_command(
    state_dir: Path = typer.Option(..., "--state-dir"),
    job_id: str = typer.Option(..., "--job-id"),
    exit_code: int = typer.Option(..., "--exit-code"),
    ended_at: str = typer.Option(..., "--ended-at"),
) -> None:
    """Internal runner callback that records a job exit in SQLite."""

    async def _record() -> None:
        service = ShellctlService(ShellctlConfig(state_dir=state_dir))
        await service.initialize_database()
        try:
            await service.record_runner_exit(job_id, exit_code, ended_at)
        finally:
            await service.shutdown()

    anyio.run(_record)


def main() -> None:
    """CLI entrypoint used by the console script and `python -m` invocations."""

    cli()


def _parse_listen(value: str) -> tuple[str, int]:
    if ":" not in value:
        raise typer.BadParameter("listen must use host:port format")
    host, raw_port = value.rsplit(":", 1)
    host = host.strip("[]")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise typer.BadParameter(f"invalid port: {raw_port}") from exc
    return host, port


__all__ = [
    "cli",
    "main",
    "runner_exit_command",
    "serve_command",
]

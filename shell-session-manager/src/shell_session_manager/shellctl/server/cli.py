"""Typer CLI entrypoints for shellctl server transports and helpers.

`shellctl serve` is the server entrypoint. It defaults to HTTP for backwards
compatibility and accepts `--transport grpc` when callers want the grpclib
transport instead. The default listen port also follows the selected transport
so HTTP keeps `127.0.0.1:8765` while gRPC uses `127.0.0.1:8766`.
"""

from __future__ import annotations

import sys
from enum import StrEnum
from pathlib import Path

import anyio
import typer
import uvicorn

from shell_session_manager.shellctl.server.api import create_app
from shell_session_manager.shellctl.server.config import ShellctlConfig
from shell_session_manager.shellctl.server.grpc import run_grpc_server
from shell_session_manager.shellctl.server.listen import parse_listen
from shell_session_manager.shellctl.server.service import ShellctlService
from shell_session_manager.shellctl.shared import (
    DEFAULT_AUTH_TOKEN_ENV,
    DEFAULT_GC_FINISHED_JOB_RETENTION_SECONDS,
    DEFAULT_GC_INTERVAL_SECONDS,
    default_state_dir,
    sanitize_pty_stream,
)

cli = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)


class ShellctlServeTransport(StrEnum):
    """Supported server transports for the primary `shellctl serve` command."""

    HTTP = "http"
    GRPC = "grpc"


@cli.command("serve")
def serve_command(
    transport: ShellctlServeTransport = typer.Option(
        ShellctlServeTransport.HTTP,
        "--transport",
        help="Server transport to run. Defaults to HTTP for backwards compatibility.",
    ),
    listen: str | None = None,
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
    """Run the shellctl server with the selected transport.

    `shellctl serve` keeps HTTP as the default transport so existing commands
    still work unchanged. Use `--transport grpc` to start the grpclib server.
    When `--listen` is not provided, the selected transport chooses its
    conventional default port.
    """

    resolved_listen = listen or _default_listen(transport)
    host, port = parse_listen(resolved_listen)
    config = ShellctlConfig(
        listen=resolved_listen,
        auth_token=auth_token,
        state_dir=state_dir or default_state_dir(),
        runtime_dir=runtime_dir,
        gc_interval_seconds=gc_interval_seconds,
        gc_finished_job_retention_seconds=gc_finished_job_retention_seconds,
    )
    if transport is ShellctlServeTransport.GRPC:
        anyio.run(run_grpc_server, config)
        return
    uvicorn.run(create_app(config), host=host, port=port, log_level="info")


def _default_listen(transport: ShellctlServeTransport) -> str:
    """Return the conventional listen address for the selected transport."""

    if transport is ShellctlServeTransport.GRPC:
        return "127.0.0.1:8766"
    return "127.0.0.1:8765"


@cli.command("sanitize-pty")
def sanitize_pty_command(
    ready_file: Path | None = typer.Option(None, "--ready-file"),
) -> None:
    """Read raw PTY bytes from stdin and write sanitized UTF-8 text to stdout."""

    if ready_file is not None:
        ready_file.touch()
    sanitize_pty_stream(sys.stdin.buffer, sys.stdout.buffer)


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


__all__ = [
    "cli",
    "main",
    "runner_exit_command",
    "sanitize_pty_command",
    "serve_command",
]

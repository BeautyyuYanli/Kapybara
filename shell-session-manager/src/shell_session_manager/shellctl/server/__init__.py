"""shellctl server package.

The original monolithic `shellctl.server` module is now organized as a package
with smaller files for configuration, SQLite models, tmux control, lifecycle
service logic, FastAPI wiring, and CLI commands. This `__init__` keeps the
common public-ish imports stable without re-exporting unrelated internals.
"""

from shell_session_manager.shellctl.server.api import create_app
from shell_session_manager.shellctl.server.cli import (
    cli,
    main,
    runner_exit_command,
    serve_command,
)
from shell_session_manager.shellctl.server.config import ShellctlConfig
from shell_session_manager.shellctl.server.db import JobRow
from shell_session_manager.shellctl.server.errors import ShellctlServerError
from shell_session_manager.shellctl.server.service import ShellctlService

__all__ = [
    "JobRow",
    "ShellctlConfig",
    "ShellctlServerError",
    "ShellctlService",
    "cli",
    "create_app",
    "main",
    "runner_exit_command",
    "serve_command",
]

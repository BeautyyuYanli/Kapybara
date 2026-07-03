"""shellctl server package.

The original monolithic `shellctl.server` module is now organized as a package
with smaller files for configuration, SQLite models, tmux control, lifecycle
service logic, FastAPI wiring, and CLI commands. This `__init__` keeps the
common public-ish imports stable without forcing light consumers such as config
readers to import the API, CLI, and service stacks eagerly.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shell_session_manager.shellctl.server.api import create_app
    from shell_session_manager.shellctl.server.cli import (
        cli,
        main,
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
    "serve_command",
]

_EXPORTS = {
    "JobRow": "shell_session_manager.shellctl.server.db",
    "ShellctlConfig": "shell_session_manager.shellctl.server.config",
    "ShellctlServerError": "shell_session_manager.shellctl.server.errors",
    "ShellctlService": "shell_session_manager.shellctl.server.service",
    "cli": "shell_session_manager.shellctl.server.cli",
    "create_app": "shell_session_manager.shellctl.server.api",
    "main": "shell_session_manager.shellctl.server.cli",
    "serve_command": "shell_session_manager.shellctl.server.cli",
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = import_module(_EXPORTS[name])
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))

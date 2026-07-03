"""Compatibility shim for historical `shellctl.server.cli` imports.

Job-management commands now live in `shell_session_manager.shellctl.cli`. This
module is intentionally a thin legacy re-export for callers that still import
CLI symbols from `shellctl.server.cli`.
"""

from __future__ import annotations

from shell_session_manager.shellctl.cli import cli, main
from shell_session_manager.shellctl.server.serve import serve_command

__all__ = [
    "cli",
    "main",
    "serve_command",
]

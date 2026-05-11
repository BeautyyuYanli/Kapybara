"""Compatibility exports for the standalone `shell-session-manager` package.

The implementation moved out of kapybara core so it can be reused independently.
New core code should import from `shell_session_manager` directly; this module
keeps the historical `k.io_helpers.shell` import path working for external
callers during the transition.
"""

from shell_session_manager import (
    NextResult,
    ShellSession,
    ShellSessionInfo,
    ShellSessionManager,
    ShellSessionOptions,
    StreamName,
    command_slug_parts,
    random_6digits,
)

__all__ = [
    "NextResult",
    "ShellSession",
    "ShellSessionInfo",
    "ShellSessionManager",
    "ShellSessionOptions",
    "StreamName",
    "command_slug_parts",
    "random_6digits",
]

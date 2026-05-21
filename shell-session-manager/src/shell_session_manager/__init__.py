"""Public API for async shell/subprocess session management.

The package exports the session primitives from `shell_session_manager.session`.
Callers are responsible for constructing the command string; this package only
owns process lifetime, incremental stdin writes, stdout/stderr draining, and
session registry management.

The higher-level HTTP/tmux shellctl implementation lives under
`shell_session_manager.shellctl` so projects that only need in-process shell
sessions do not need to import the networked manager.
"""

from shell_session_manager.session import (
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

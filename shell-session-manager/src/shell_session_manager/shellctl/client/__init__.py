"""Async HTTP client package for shellctl.

`shell_session_manager.shellctl.client` remains importable as before, but it is
 now a package so future client helpers can live beside the main SDK class.
"""

from shell_session_manager.shellctl.client.sdk import (
    ShellctlClient,
    ShellctlClientError,
)

__all__ = ["ShellctlClient", "ShellctlClientError"]

"""Public shellctl package exports.

This package stays lazy on purpose. Hot-path runtime helpers live outside the
`shell_session_manager.shellctl` package, and importing this package root should
not pull the full client/server/public DTO surface unless a caller explicitly
asks for those exports.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shell_session_manager.shellctl.client import (
        ShellctlClient,
        ShellctlClientError,
    )
    from shell_session_manager.shellctl.shared import (
        DEFAULT_AUTH_TOKEN_ENV,
        DEFAULT_BASE_URL,
        DEFAULT_BASE_URL_ENV,
        DEFAULT_GC_FINISHED_JOB_RETENTION_SECONDS,
        DEFAULT_GC_INTERVAL_SECONDS,
        DEFAULT_IDLE_FLUSH_SECONDS,
        DEFAULT_LIST_LIMIT,
        DEFAULT_OUTPUT_LIMIT_BYTES,
        DEFAULT_TERMINAL_COLS,
        DEFAULT_TERMINAL_ROWS,
        DEFAULT_TERMINATE_GRACE_SECONDS,
        DEFAULT_TIMEOUT_SECONDS,
        DeleteJobResponse,
        HealthResponse,
        InputJobRequest,
        JobInfo,
        JobResult,
        JobStatusName,
        JobStatusView,
        ListJobsResponse,
        RunJobRequest,
        TerminalSize,
        TerminateJobRequest,
        WaitJobRequest,
        generate_job_id,
        read_output_window,
        tail_output_window,
    )

__all__ = [
    "DEFAULT_AUTH_TOKEN_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_BASE_URL_ENV",
    "DEFAULT_GC_FINISHED_JOB_RETENTION_SECONDS",
    "DEFAULT_GC_INTERVAL_SECONDS",
    "DEFAULT_IDLE_FLUSH_SECONDS",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_OUTPUT_LIMIT_BYTES",
    "DEFAULT_TERMINAL_COLS",
    "DEFAULT_TERMINAL_ROWS",
    "DEFAULT_TERMINATE_GRACE_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DeleteJobResponse",
    "HealthResponse",
    "InputJobRequest",
    "JobInfo",
    "JobResult",
    "JobStatusName",
    "JobStatusView",
    "ListJobsResponse",
    "RunJobRequest",
    "ShellctlClient",
    "ShellctlClientError",
    "TerminalSize",
    "TerminateJobRequest",
    "WaitJobRequest",
    "generate_job_id",
    "read_output_window",
    "tail_output_window",
]

_EXPORTS = {
    "ShellctlClient": "shell_session_manager.shellctl.client",
    "ShellctlClientError": "shell_session_manager.shellctl.client",
    "DEFAULT_AUTH_TOKEN_ENV": "shell_session_manager.shellctl.shared",
    "DEFAULT_BASE_URL": "shell_session_manager.shellctl.shared",
    "DEFAULT_BASE_URL_ENV": "shell_session_manager.shellctl.shared",
    "DEFAULT_GC_FINISHED_JOB_RETENTION_SECONDS": "shell_session_manager.shellctl.shared",
    "DEFAULT_GC_INTERVAL_SECONDS": "shell_session_manager.shellctl.shared",
    "DEFAULT_IDLE_FLUSH_SECONDS": "shell_session_manager.shellctl.shared",
    "DEFAULT_LIST_LIMIT": "shell_session_manager.shellctl.shared",
    "DEFAULT_OUTPUT_LIMIT_BYTES": "shell_session_manager.shellctl.shared",
    "DEFAULT_TERMINAL_COLS": "shell_session_manager.shellctl.shared",
    "DEFAULT_TERMINAL_ROWS": "shell_session_manager.shellctl.shared",
    "DEFAULT_TERMINATE_GRACE_SECONDS": "shell_session_manager.shellctl.shared",
    "DEFAULT_TIMEOUT_SECONDS": "shell_session_manager.shellctl.shared",
    "DeleteJobResponse": "shell_session_manager.shellctl.shared",
    "HealthResponse": "shell_session_manager.shellctl.shared",
    "InputJobRequest": "shell_session_manager.shellctl.shared",
    "JobInfo": "shell_session_manager.shellctl.shared",
    "JobResult": "shell_session_manager.shellctl.shared",
    "JobStatusName": "shell_session_manager.shellctl.shared",
    "JobStatusView": "shell_session_manager.shellctl.shared",
    "ListJobsResponse": "shell_session_manager.shellctl.shared",
    "RunJobRequest": "shell_session_manager.shellctl.shared",
    "TerminalSize": "shell_session_manager.shellctl.shared",
    "TerminateJobRequest": "shell_session_manager.shellctl.shared",
    "WaitJobRequest": "shell_session_manager.shellctl.shared",
    "generate_job_id": "shell_session_manager.shellctl.shared",
    "read_output_window": "shell_session_manager.shellctl.shared",
    "tail_output_window": "shell_session_manager.shellctl.shared",
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

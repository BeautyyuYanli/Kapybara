"""Shared shellctl transport/runtime helpers.

This package preserves the historical import surface while keeping the package
root lazy. Lightweight callers can import concrete submodules such as
`shared.runtime` without eagerly importing the pydantic schema layer, output
or helpers outside the shared compatibility surface.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from shell_session_manager.shellctl.shared.constants import (
        DEFAULT_AUTH_TOKEN_ENV,
        DEFAULT_BASE_URL,
        DEFAULT_BASE_URL_ENV,
        DEFAULT_GC_FINISHED_JOB_RETENTION_SECONDS,
        DEFAULT_GC_INTERVAL_SECONDS,
        DEFAULT_HEALTH_STATUS,
        DEFAULT_IDLE_FLUSH_SECONDS,
        DEFAULT_LIST_LIMIT,
        DEFAULT_OUTPUT_LIMIT_BYTES,
        DEFAULT_TERMINAL_COLS,
        DEFAULT_TERMINAL_ROWS,
        DEFAULT_TERMINATE_GRACE_SECONDS,
        DEFAULT_TIMEOUT_SECONDS,
        JOB_ID_ALPHABET,
        JOB_ID_RANDOM_SUFFIX_LENGTH,
        MAX_LIST_LIMIT,
        MAX_OUTPUT_LIMIT_BYTES,
        MAX_WAIT_TIMEOUT_SECONDS,
        SESSION_NAME_PREFIX,
    )
    from shell_session_manager.shellctl.shared.output import (
        OutputWindow,
        read_output_window,
        tail_output_window,
    )
    from shell_session_manager.shellctl.shared.runtime import (
        default_runtime_dir,
        default_state_dir,
        format_timestamp,
        generate_job_id,
        is_terminal_status,
        job_pane_target,
        job_session_name,
        parse_timestamp,
        utc_now,
    )
    from shell_session_manager.shellctl.shared.schemas import (
        TERMINAL_JOB_STATUSES,
        DeleteJobResponse,
        ErrorDetail,
        ErrorResponse,
        HealthResponse,
        InputJobRequest,
        JobInfo,
        JobResult,
        JobStatusName,
        JobStatusView,
        ListJobsResponse,
        RunJobRequest,
        ShellctlModel,
        TerminalSize,
        TerminateJobRequest,
        WaitJobRequest,
    )

__all__ = [
    "DEFAULT_AUTH_TOKEN_ENV",
    "DEFAULT_BASE_URL",
    "DEFAULT_BASE_URL_ENV",
    "DEFAULT_GC_FINISHED_JOB_RETENTION_SECONDS",
    "DEFAULT_GC_INTERVAL_SECONDS",
    "DEFAULT_HEALTH_STATUS",
    "DEFAULT_IDLE_FLUSH_SECONDS",
    "DEFAULT_LIST_LIMIT",
    "DEFAULT_OUTPUT_LIMIT_BYTES",
    "DEFAULT_TERMINAL_COLS",
    "DEFAULT_TERMINAL_ROWS",
    "DEFAULT_TERMINATE_GRACE_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "JOB_ID_ALPHABET",
    "JOB_ID_RANDOM_SUFFIX_LENGTH",
    "MAX_LIST_LIMIT",
    "MAX_OUTPUT_LIMIT_BYTES",
    "MAX_WAIT_TIMEOUT_SECONDS",
    "SESSION_NAME_PREFIX",
    "TERMINAL_JOB_STATUSES",
    "DeleteJobResponse",
    "ErrorDetail",
    "ErrorResponse",
    "HealthResponse",
    "InputJobRequest",
    "JobInfo",
    "JobResult",
    "JobStatusName",
    "JobStatusView",
    "ListJobsResponse",
    "OutputWindow",
    "RunJobRequest",
    "ShellctlModel",
    "TerminalSize",
    "TerminateJobRequest",
    "WaitJobRequest",
    "default_runtime_dir",
    "default_state_dir",
    "format_timestamp",
    "generate_job_id",
    "is_terminal_status",
    "job_pane_target",
    "job_session_name",
    "parse_timestamp",
    "read_output_window",
    "tail_output_window",
    "utc_now",
]

_EXPORTS = {
    "DEFAULT_AUTH_TOKEN_ENV": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_BASE_URL": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_BASE_URL_ENV": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_GC_FINISHED_JOB_RETENTION_SECONDS": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_GC_INTERVAL_SECONDS": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_HEALTH_STATUS": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_IDLE_FLUSH_SECONDS": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_LIST_LIMIT": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_OUTPUT_LIMIT_BYTES": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_TERMINAL_COLS": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_TERMINAL_ROWS": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_TERMINATE_GRACE_SECONDS": "shell_session_manager.shellctl.shared.constants",
    "DEFAULT_TIMEOUT_SECONDS": "shell_session_manager.shellctl.shared.constants",
    "JOB_ID_ALPHABET": "shell_session_manager.shellctl.shared.constants",
    "JOB_ID_RANDOM_SUFFIX_LENGTH": "shell_session_manager.shellctl.shared.constants",
    "MAX_LIST_LIMIT": "shell_session_manager.shellctl.shared.constants",
    "MAX_OUTPUT_LIMIT_BYTES": "shell_session_manager.shellctl.shared.constants",
    "MAX_WAIT_TIMEOUT_SECONDS": "shell_session_manager.shellctl.shared.constants",
    "SESSION_NAME_PREFIX": "shell_session_manager.shellctl.shared.constants",
    "OutputWindow": "shell_session_manager.shellctl.shared.output",
    "read_output_window": "shell_session_manager.shellctl.shared.output",
    "tail_output_window": "shell_session_manager.shellctl.shared.output",
    "default_runtime_dir": "shell_session_manager.shellctl.shared.runtime",
    "default_state_dir": "shell_session_manager.shellctl.shared.runtime",
    "format_timestamp": "shell_session_manager.shellctl.shared.runtime",
    "generate_job_id": "shell_session_manager.shellctl.shared.runtime",
    "is_terminal_status": "shell_session_manager.shellctl.shared.runtime",
    "job_pane_target": "shell_session_manager.shellctl.shared.runtime",
    "job_session_name": "shell_session_manager.shellctl.shared.runtime",
    "parse_timestamp": "shell_session_manager.shellctl.shared.runtime",
    "utc_now": "shell_session_manager.shellctl.shared.runtime",
    "TERMINAL_JOB_STATUSES": "shell_session_manager.shellctl.shared.schemas",
    "DeleteJobResponse": "shell_session_manager.shellctl.shared.schemas",
    "ErrorDetail": "shell_session_manager.shellctl.shared.schemas",
    "ErrorResponse": "shell_session_manager.shellctl.shared.schemas",
    "HealthResponse": "shell_session_manager.shellctl.shared.schemas",
    "InputJobRequest": "shell_session_manager.shellctl.shared.schemas",
    "JobInfo": "shell_session_manager.shellctl.shared.schemas",
    "JobResult": "shell_session_manager.shellctl.shared.schemas",
    "JobStatusName": "shell_session_manager.shellctl.shared.schemas",
    "JobStatusView": "shell_session_manager.shellctl.shared.schemas",
    "ListJobsResponse": "shell_session_manager.shellctl.shared.schemas",
    "RunJobRequest": "shell_session_manager.shellctl.shared.schemas",
    "ShellctlModel": "shell_session_manager.shellctl.shared.schemas",
    "TerminalSize": "shell_session_manager.shellctl.shared.schemas",
    "TerminateJobRequest": "shell_session_manager.shellctl.shared.schemas",
    "WaitJobRequest": "shell_session_manager.shellctl.shared.schemas",
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

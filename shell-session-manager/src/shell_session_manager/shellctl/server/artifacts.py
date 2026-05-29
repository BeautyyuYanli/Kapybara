"""Per-job artifact names used by shellctl server/runtime code.

Normal job completion is coordinated through small marker files inside each
`jobs/<job_id>/` directory so the tmux output-pipe finalizer can publish the
SQLite `exited(exit_code, ended_at)` state only after PTY output is fully
drained into `output.log`. A separate failure marker is used so an unsuccessful
sanitizer run does not masquerade as a drained normal exit.
"""

from __future__ import annotations

from pathlib import Path

RUNNER_EXIT_CODE_FILENAME = ".runner-exit-code"
RUNNER_ENDED_AT_FILENAME = ".runner-ended-at"
PIPE_DRAINED_FILENAME = ".pipe-drained"
PIPE_FAILED_FILENAME = ".pipe-failed"


def runner_exit_code_path(job_dir: Path) -> Path:
    return job_dir / RUNNER_EXIT_CODE_FILENAME


def runner_ended_at_path(job_dir: Path) -> Path:
    return job_dir / RUNNER_ENDED_AT_FILENAME


def pipe_drained_path(job_dir: Path) -> Path:
    return job_dir / PIPE_DRAINED_FILENAME


def pipe_failed_path(job_dir: Path) -> Path:
    return job_dir / PIPE_FAILED_FILENAME


__all__ = [
    "PIPE_DRAINED_FILENAME",
    "PIPE_FAILED_FILENAME",
    "RUNNER_ENDED_AT_FILENAME",
    "RUNNER_EXIT_CODE_FILENAME",
    "pipe_drained_path",
    "pipe_failed_path",
    "runner_ended_at_path",
    "runner_exit_code_path",
]

"""Public values for local process execution; no ORM objects or command secrets escape."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from uuid import UUID

type ProcessMode = Literal["stdio", "pty"]
type ProcessState = Literal["starting", "running", "terminating", "exited", "failed", "lost"]
type ResourceState = Literal["active", "deleting"]
type OutputState = Literal["collecting", "complete", "incomplete"]
type OutputStream = Literal["stdout", "stderr", "pty"]
type ErrorCode = Literal["invalid_argument", "not_found", "conflict", "gone", "closed", "io_error"]


class ProcessError(Exception):
    """A safe, transport-independent operation failure."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True, kw_only=True)
class TerminalSize:
    rows: int = 24
    columns: int = 80


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessSpec:
    argv: tuple[str, ...]
    cwd: str
    env: dict[str, str] = field(default_factory=dict, repr=False)
    mode: ProcessMode = "stdio"
    terminal_size: TerminalSize | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessStatus:
    process_id: UUID
    mode: ProcessMode
    cwd: str
    state: ProcessState
    resource_state: ResourceState
    output_state: OutputState
    created_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessPage:
    items: tuple[ProcessStatus, ...]
    next_after: UUID | None


@dataclass(frozen=True, slots=True, kw_only=True)
class OutputPage:
    stream: OutputStream
    data: bytes
    next_offset: int
    available_end: int
    eof: bool

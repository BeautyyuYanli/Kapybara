"""Approved State value objects and injected runner contract."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
type InputMode = Literal["steer", "queue"]
type Cursor = str


@dataclass(frozen=True, slots=True)
class RunnerState:
    codec: str
    data: JsonObject


@dataclass(frozen=True, slots=True)
class SessionSpec:
    title: str
    machine_ids: tuple[str, ...]
    default_machine_id: str | None
    config: JsonObject
    initial_state: RunnerState


@dataclass(frozen=True, slots=True)
class SessionView:
    id: UUID
    title: str
    machine_ids: tuple[str, ...]
    default_machine_id: str | None
    config: JsonObject
    status: Literal["waiting", "running", "deleting"]
    run_id: UUID | None
    cursor: Cursor
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Submission:
    request_id: UUID
    session_id: UUID
    input_id: UUID | None
    waiting_id: UUID


@dataclass(frozen=True, slots=True)
class CreatedSession:
    session: SessionView
    submission: Submission


@dataclass(frozen=True, slots=True)
class Completion:
    run_id: UUID | None
    outcome: Literal["completed", "failed", "deleted"]
    output: str
    cursor: Cursor
    completed_at: datetime


@dataclass(frozen=True, slots=True)
class SubmissionStatus:
    submission: Submission
    completion: Completion | None


@dataclass(frozen=True, slots=True)
class SessionInput:
    id: UUID
    seq: int
    mode: InputMode
    payload: JsonValue
    event_id: UUID | None


@dataclass(frozen=True, slots=True)
class MessageWrite:
    message_id: UUID
    kind: Literal["model_request", "model_response"]
    text: str
    data: JsonObject


@dataclass(frozen=True, slots=True)
class CheckpointWrite:
    number: int
    state: RunnerState
    messages: tuple[MessageWrite, ...]
    consumed_input_ids: tuple[UUID, ...]


@dataclass(frozen=True, slots=True)
class OutputDelta:
    emission_id: UUID
    message_id: UUID
    kind: Literal["text_delta", "tool_call", "tool_result", "notice"]
    data: JsonValue


@dataclass(frozen=True, slots=True)
class Record:
    cursor: Cursor
    run_id: UUID | None
    attempt: int | None
    message_id: UUID | None
    kind: str
    data: JsonValue
    text: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RecordPage:
    items: tuple[Record, ...]
    next_cursor: Cursor
    has_more: bool


@dataclass(frozen=True, slots=True)
class HistoryExportPage:
    items: tuple[Record, ...]
    next_cursor: Cursor
    snapshot_cursor: Cursor
    has_more: bool


@dataclass(frozen=True, slots=True)
class SessionPage:
    items: tuple[SessionView, ...]
    next_after: UUID | None


@dataclass(frozen=True, slots=True)
class EventReceipt:
    request_id: UUID
    event_id: UUID
    waiting_id: UUID
    delivered: int
    pending: bool


@dataclass(frozen=True, slots=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple[JsonValue, ...], ...]
    truncated: bool


class RunContext(Protocol):
    session: SessionView
    run_id: UUID
    attempt: int
    recovered: bool
    inputs: tuple[SessionInput, ...]
    state: RunnerState
    checkpoint_number: int

    async def poll_steer(self, *, limit: int = 64) -> tuple[SessionInput, ...]: ...
    async def emit(self, delta: OutputDelta) -> Cursor: ...
    async def checkpoint(self, write: CheckpointWrite) -> Cursor: ...
    async def read_history(
        self, *, after: Cursor | None = None, limit: int = 200
    ) -> RecordPage: ...


@dataclass(frozen=True, slots=True)
class RunResult:
    output: str
    wait_for: tuple[UUID, ...]
    checkpoint: CheckpointWrite


type SessionRunner = Callable[[RunContext], Awaitable[RunResult]]


class StateError(Exception):
    """Base class for State failures safe to map at an API boundary."""


class NotFound(StateError):
    """The requested resource does not exist in this session."""


class Conflict(StateError):
    """The operation conflicts with durable state or an idempotency key."""


class InvalidArgument(StateError):
    """A business argument or bounded payload is invalid."""


class UnsafeQuery(StateError):
    """History SQL is outside the permitted SELECT language."""


class QueryLimitExceeded(StateError):
    """A history query exceeded a bounded execution or result limit."""


class ServiceUnavailable(StateError):
    """State is closed or no longer owns the control-process lease."""

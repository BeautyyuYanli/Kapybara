"""Runner values, normalized history and transient events; no transport or session CRUD."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, JsonValue
from pydantic_ai.messages import ModelMessage, UserContent
from sqlalchemy.ext.asyncio import AsyncSession

from kapy.session_lease import LeaseLost
from kapy.session_lease import SessionBusy as SessionBusy

type UserInput = str | Sequence[UserContent]
type NextStep = Literal["model_request", "handle_response", "done"]
type ConsumeInputs = Callable[[AsyncSession], Awaitable[tuple[UserInput, ...]]]
type ConsumeCancel = Callable[[AsyncSession], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    """One original history row, using the same normalized message codec as storage."""

    session_id: UUID
    seq: int
    message: ModelMessage


@dataclass(frozen=True, slots=True)
class TextDelta:
    """Provisional text for one response part; its committed message replaces it.

    replace initializes or clears a part, append adds text without trimming.
    Different execution attempts can share response_seq; deltas are not resumable.
    """

    session_id: UUID
    response_seq: int
    part_index: int
    part_kind: Literal["text", "thinking"]
    op: Literal["replace", "append"]
    text: str
    type: Literal["delta"] = "delta"


@dataclass(frozen=True, slots=True)
class MessageCommitted:
    """A complete durable message, not a notification that execution has finished."""

    message: HistoryMessage
    type: Literal["message"] = "message"


type OutputEvent = Annotated[TextDelta | MessageCommitted, Field(discriminator="type")]
type OutputCallback = Callable[[OutputEvent], Awaitable[None]]


# Compatibility name: ownership belongs to the shared lease, not only runners.
RunnerLost = LeaseLost


@dataclass(frozen=True, slots=True)
class InputBatch:
    """Nonempty read snapshot; consume borrows the runner's fenced transaction.

    inputs are candidates for SDK preparation. consume deletes only this snapshot's
    rows and returns the actually accepted contents in the same order. Concurrent
    withdrawal can only shrink that result: rows are immutable and IDs not reused.
    It must not commit, open another transaction, or access an external queue.
    Preparation may retry consumption after rolling back when candidates shrink.
    """

    inputs: tuple[UserInput, ...]
    consume: ConsumeInputs

    def __post_init__(self) -> None:
        if not self.inputs:
            raise ValueError("InputBatch must contain at least one input")


type ReadInputs = Callable[[], Awaitable[InputBatch | None]]


@dataclass(frozen=True, slots=True)
class TurnResult[OutputT]:
    """finished reflects the current done state, not whether input ever existed.

    output contains a final result produced during this call, if any. A run keeps
    its just-produced output when the following boundary has no input or consumes
    cancel while still done. An empty turn, or reopening an already-done session
    without new input, returns output=None; historical output is not reconstructed.
    """

    finished: bool
    output: OutputT | None = None


@dataclass(frozen=True, slots=True)
class ContextPageRecord:
    """Durable strategy state at an original-history anchor; not a checkpoint."""

    anchor_seq: int
    policy_key: str
    payload: dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class ResumeState:
    next_step: NextStep
    next_seq: int
    page: ContextPageRecord | None

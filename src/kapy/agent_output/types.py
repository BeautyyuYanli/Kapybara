"""Transient previews and complete-message broadcasts; deltas have no replay cursor."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from kapy.agent_history import HistoryMessage


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

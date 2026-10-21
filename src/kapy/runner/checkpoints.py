"""Runner-owned checkpoint codec and immutable-history admission checks.

The envelope stores only a business boundary, complete SDK messages and its next
action. Native writer identity stays in sessions; SDK run identity stays inside
messages. Legacy arrays are context for a new prompt, never resumable results.
"""

import json
from collections.abc import Sequence
from typing import Annotated, Literal, Self

from pydantic import Field, field_serializer, field_validator, model_validator
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
)

from kapy.agent_history import HistoryMessage
from kapy.agent_history.repository import history_projection
from kapy.control.types import DTO

type CheckpointKind = Literal[
    "before_model_request", "model_node_completed", "tools_node_completed", "run_completed"
]


class ModelRequestNext(DTO):
    kind: Literal["model_request"] = "model_request"


class CallToolsNext(DTO):
    kind: Literal["call_tools"] = "call_tools"


class FinishNext(DTO):
    kind: Literal["finish"] = "finish"
    output: str
    tool_name: str | None = None
    tool_call_id: str | None = None


class DoneNext(DTO):
    kind: Literal["done"] = "done"
    output: str


type Next = Annotated[
    ModelRequestNext | CallToolsNext | FinishNext | DoneNext, Field(discriminator="kind")
]


def marked_prefix(messages: Sequence[ModelMessage], *, complete: bool) -> int:
    """Validate increasing nonnegative positions; unmarked legacy messages form a suffix."""
    boundary, unmarked, count = -1, False, 0
    for message in messages:
        if message.state != "complete":
            raise ValueError("Checkpoint messages must be complete")
        metadata = message.metadata or {}
        if "seq" not in metadata:
            if complete:
                raise ValueError("Checkpoint messages require metadata.seq")
            unmarked = True
            continue
        seq = metadata["seq"]
        if type(seq) is not int or seq < 0:
            raise ValueError("History metadata.seq must be a nonnegative integer")
        if unmarked:
            raise ValueError("Unmarked history messages must form a suffix")
        if seq <= boundary:
            raise ValueError("History seq must be strictly increasing")
        boundary, count = seq, count + 1
    return count


def message_boundary(messages: Sequence[ModelMessage]) -> int:
    marked_prefix(messages, complete=True)
    return (messages[-1].metadata or {})["seq"] if messages else -1


class Checkpoint(DTO):
    version: Literal[1] = 1
    checkpoint: CheckpointKind
    messages: list[ModelMessage]
    next: Next

    @field_validator("version", mode="before")
    @classmethod
    def version_is_integer(cls, value):
        if type(value) is not int:
            raise ValueError("Checkpoint version must be an integer")
        return value

    @field_validator("messages", mode="before")
    @classmethod
    def decode_messages(cls, value):
        return ModelMessagesTypeAdapter.validate_python(value)

    @field_serializer("messages")
    def encode_messages(self, value):
        return ModelMessagesTypeAdapter.dump_python(value, mode="json")

    @model_validator(mode="after")
    def validate_boundary(self) -> Self:
        message_boundary(self.messages)
        allowed = {
            "before_model_request": {"model_request"},
            "model_node_completed": {"call_tools", "model_request"},
            "tools_node_completed": {"model_request", "finish"},
            "run_completed": {"done"},
        }
        if self.next.kind not in allowed[self.checkpoint]:
            raise ValueError("Checkpoint and next action disagree")
        if self.next.kind == "model_request" and (
            not self.messages or not isinstance(self.messages[-1], ModelRequest)
        ):
            raise ValueError("Model request checkpoint must end in a complete request")
        if self.next.kind == "call_tools" and (
            not self.messages
            or not isinstance(self.messages[-1], ModelResponse)
            or self.messages[-1].state != "complete"
        ):
            raise ValueError("Tools checkpoint must end in a complete response")
        return self


type StoredState = Checkpoint | list[ModelMessage] | None


def decode_state(value: str | None) -> StoredState:
    """Reject unknown envelopes instead of guessing completion from message shape."""
    if value is None:
        return None
    raw = json.loads(value)
    if isinstance(raw, list):
        messages = ModelMessagesTypeAdapter.validate_json(value)
        marked_prefix(messages, complete=False)
        return messages
    if not isinstance(raw, dict) or "version" not in raw:
        raise ValueError("Invalid runner checkpoint envelope")
    return Checkpoint.model_validate(raw)


def state_messages(state: StoredState) -> list[ModelMessage]:
    return state.messages if isinstance(state, Checkpoint) else state or []


def require_new_prompt(state: StoredState) -> None:
    from kapy.control.sessions.types import SessionBusy

    if isinstance(state, Checkpoint) and not isinstance(state.next, DoneNext):
        raise SessionBusy("Unfinished checkpoint requires explicit resume or close")


def validate_history(state: StoredState, entries: Sequence[HistoryMessage]) -> None:
    """Match every sealed position and codec projection, preserving all numeric gaps.

    Legacy unnumbered tails may be appended at the first checkpoint of a new
    prompt. History ahead of that prefix, including NULL with history, is unsafe.
    """
    messages = state_messages(state)
    count = marked_prefix(messages, complete=isinstance(state, Checkpoint))
    if count != len(entries):
        raise ValueError("Runner checkpoint and history boundary disagree")
    for message, entry in zip(messages[:count], entries, strict=True):
        if (message.metadata or {})["seq"] != entry.seq or history_projection(
            message
        ) != history_projection(entry.message):
            raise ValueError("Runner checkpoint and immutable history disagree")

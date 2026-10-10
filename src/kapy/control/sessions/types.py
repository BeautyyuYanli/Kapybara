"""Public fixed layer Config, legacy plugin specs, lifecycle and user inputs.

Execution State stays internal. Layer names/configuration cannot change through
UpdateSession, and nonempty layers/plugins select mutually exclusive runners.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from kapy.agent_plugins.contracts import PluginSpec
from kapy.agent_runner.types import UserInput
from kapy.context_plugins.registry import ContextPluginSpec
from kapy.control.types import DTO, JsonObject, Name, UpdateDTO
from kapy.lifecycle import LifecycleStatus

type InputChannel = Literal["steer", "queued"]


@dataclass(frozen=True, slots=True)
class SessionInput:
    id: int
    content: UserInput


class SubmitInput(DTO):
    content: UserInput
    channel: InputChannel = "queued"


class InputSubmission(DTO):
    """The start intention observes a lease; it does not reserve execution."""

    input: SessionInput
    should_start_runner: bool


class CreateSession(DTO):
    """Fixed execution configuration; optional id lets callers resolve lost responses.

    Reusing an existing/reserved identity conflicts instead of replaying enter or
    initial input. The caller queries or closes that same identity explicitly.
    """

    id: UUID | None = None
    plugins: list[PluginSpec] = Field(default_factory=list)
    layers: dict[str, JsonObject] = Field(default_factory=dict)
    context_plugin: ContextPluginSpec = Field(default_factory=ContextPluginSpec)
    provider_id: UUID
    model_name: Name
    title: str = Field(default="", max_length=256)
    model_settings: JsonObject = Field(default_factory=dict)
    compaction_threshold_tokens: int | None = Field(default=None, gt=0, strict=True)
    compaction_replay_turns: int = Field(default=10, ge=0, strict=True)

    @model_validator(mode="after")
    def choose_runner_components(self) -> Self:
        if self.layers and self.plugins:
            raise ValueError("layers and legacy plugins cannot be configured together")
        return self


class UpdateSession(UpdateDTO):
    nullable_fields = frozenset({"compaction_threshold_tokens"})
    context_plugin: ContextPluginSpec | None = None
    title: str | None = Field(default=None, max_length=256)
    provider_id: UUID | None = None
    model_name: Name | None = None
    model_settings: JsonObject | None = None
    compaction_threshold_tokens: int | None = Field(default=None, gt=0, strict=True)
    compaction_replay_turns: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def require_model_pair(self) -> Self:
        if ("provider_id" in self.model_fields_set) != ("model_name" in self.model_fields_set):
            raise ValueError("provider_id and model_name must be updated together")
        return self


class SessionRecord(DTO):
    layers: dict[str, JsonObject] = Field(default_factory=dict)
    context_plugin: ContextPluginSpec
    status: LifecycleStatus
    id: UUID
    title: str
    provider_id: UUID
    model_name: str
    model_settings: JsonObject
    compaction_threshold_tokens: int | None
    compaction_replay_turns: int
    created_at: datetime
    updated_at: datetime


class SessionAlreadyExists(RuntimeError):
    """A fixed identity is already registered or owned; never replay creation."""

    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
        super().__init__(f"Session {session_id} already exists or is being created")


class SessionCreationError(RuntimeError):
    """Creation failed after reservation began; query/close this identity explicitly.

    The original error remains the cause for internal diagnostics. Resource or
    commit outcomes may be unconfirmed, so this exception does not imply rollback.
    """

    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
        super().__init__(f"Unable to complete creation of session {session_id}")

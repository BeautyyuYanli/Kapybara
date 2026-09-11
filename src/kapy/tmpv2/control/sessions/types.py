"""Session configuration and user inputs; prompts/tools stay on the caller-owned Agent."""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from kapy.tmpv2.agent_runner.types import UserInput
from kapy.tmpv2.control.types import DTO, JsonObject, Name, UpdateDTO

type InputChannel = Literal["steer", "queued"]


@dataclass(frozen=True, slots=True)
class SessionInput:
    id: int
    content: UserInput


class CreateSession(DTO):
    provider_id: UUID
    model_name: Name
    title: str = Field(default="", max_length=256)
    model_settings: JsonObject = Field(default_factory=dict)
    compaction_threshold_tokens: int | None = Field(default=None, gt=0, strict=True)
    compaction_replay_turns: int = Field(default=10, ge=0, strict=True)


class UpdateSession(UpdateDTO):
    nullable_fields = frozenset({"compaction_threshold_tokens"})
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
    id: UUID
    title: str
    provider_id: UUID
    model_name: str
    model_settings: JsonObject
    compaction_threshold_tokens: int | None
    compaction_replay_turns: int
    created_at: datetime
    updated_at: datetime

"""Public fixed native layer Config, model selection and session lifecycle.

Execution State stays internal. Layer names/configuration are fixed on creation.
"""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from kapy.control.types import DTO, JsonObject, Name, UpdateDTO
from kapy.lifecycle import LifecycleStatus


class CreateSession(DTO):
    """Fixed execution configuration; optional id lets callers resolve lost responses.

    Reusing an existing/reserved identity conflicts instead of replaying enter or
    business hooks. The caller queries or closes that same identity explicitly.
    """

    id: UUID | None = None
    layers: dict[str, JsonObject] = Field(default_factory=dict)
    provider_id: UUID
    model_name: Name
    title: str = Field(default="", max_length=256)
    model_settings: JsonObject = Field(default_factory=dict)


class UpdateSession(UpdateDTO):
    title: str | None = Field(default=None, max_length=256)
    provider_id: UUID | None = None
    model_name: Name | None = None
    model_settings: JsonObject | None = None

    @model_validator(mode="after")
    def require_model_pair(self) -> Self:
        if ("provider_id" in self.model_fields_set) != ("model_name" in self.model_fields_set):
            raise ValueError("provider_id and model_name must be updated together")
        return self


class SessionRecord(DTO):
    layers: dict[str, JsonObject] = Field(default_factory=dict)
    status: LifecycleStatus
    id: UUID
    title: str
    provider_id: UUID
    model_name: str
    model_settings: JsonObject
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

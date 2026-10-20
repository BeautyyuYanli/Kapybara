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

    The Activity UUID is the default session ID. Temporal retains deduplication
    and results only for its retention period; the session primary key is permanent.
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
    current_activity_id: UUID | None = None
    current_activity_run_id: str | None = None
    created_at: datetime
    updated_at: datetime


class SessionAlreadyExists(RuntimeError):
    """A fixed identity is already registered or owned; never replay creation."""

    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
        super().__init__(f"Session {session_id} already exists or is being created")


class SessionActivityRequest(DTO):
    session_id: UUID


class CreateSessionActivity(SessionActivityRequest):
    request: CreateSession


class RunAgentActivity(SessionActivityRequest):
    user_prompt: str


class ActivityReference(DTO):
    """A reference to one native execution, fixed after submission or lookup."""

    activity_id: UUID
    run_id: str


class SessionActivity(ActivityReference):
    """Temporal facts; input is used only to validate ownership, never returned."""

    session_id: UUID
    activity_type: str
    status: str
    run_state: str | None
    result: str | SessionRecord | None = None
    error: str | None = None


class SessionBusy(RuntimeError):
    """A different operation currently owns the session."""


class ActivityIdentityConflict(RuntimeError):
    """A retained Activity belongs to a different session or Activity type."""

    def __init__(self, session_id: UUID, activity_id: UUID, run_id: str | None) -> None:
        self.session_id = session_id
        self.activity_id = activity_id
        self.run_id = run_id
        super().__init__("Activity identity conflict")


class SessionExecutionError(RuntimeError):
    """Safe native failure or uncertain RPC outcome, with recovery identities."""

    def __init__(
        self, session_id: UUID, activity_id: UUID, run_id: str | None, category: str
    ) -> None:
        self.session_id = session_id
        self.activity_id = activity_id
        self.run_id = run_id
        self.category = category
        super().__init__(category)


class OwnershipLost(RuntimeError):
    """A Worker may no longer issue calls or write progress for this operation."""

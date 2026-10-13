"""Public fixed native layer Config, model selection and session lifecycle.

Execution State stays internal. Layer names/configuration are fixed on creation.
"""

from datetime import datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, model_validator

from kapy.control.types import DTO, JsonObject, Name, UpdateDTO
from kapy.lifecycle import LifecycleStatus


class CreateSession(DTO):
    """Fixed execution configuration; optional id lets callers resolve lost responses.

    SessionService requires a separate activity_id. Same-key identical intent
    returns its original result; a different key cannot reuse a reserved session ID.
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


class OperationKind(StrEnum):
    CREATE = "create"
    RUN = "run"
    CLOSE = "close"


class OperationPhase(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SessionOperation(DTO):
    """Durable public outcome. Private frozen inputs never leave the repository."""

    session_id: UUID
    activity_id: UUID
    kind: OperationKind
    phase: OperationPhase
    run_id: str | None
    result: str | SessionRecord | None
    error: str | None
    created_at: datetime
    deadline: datetime


class SessionBusy(RuntimeError):
    """A different operation currently owns the session."""


class OperationConflict(RuntimeError):
    """An Activity ID was reused for different intent."""


class OperationFailed(RuntimeError):
    """The immutable safe error from a failed operation."""

    def __init__(self, operation: SessionOperation) -> None:
        self.operation = operation
        super().__init__(operation.error or "Session operation failed")


class OwnershipLost(RuntimeError):
    """A Worker may no longer issue calls or write progress for this operation."""

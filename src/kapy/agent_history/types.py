"""Complete message DTOs shared by history storage and runner output."""

from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from pydantic import Field
from pydantic_ai.messages import ModelMessage


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    """Immutable, successfully stored SDK message at a session position.

    Storage reads these attributes from columns, never from message metadata.
    """

    session_id: UUID
    seq: Annotated[int, Field(strict=True, ge=0)]
    message: ModelMessage

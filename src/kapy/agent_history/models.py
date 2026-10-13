"""Immutable complete SDK messages; columns own position, JSON owns payload.

No physical foreign keys tie history retention to session configuration or lifetime.
"""

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, MetaData, Text, func
from sqlmodel import Field, SQLModel

history_metadata = MetaData()


class AgentHistoryRow(SQLModel, table=True):
    metadata: ClassVar[MetaData] = history_metadata
    __tablename__ = "agent_history"  # pyrefly: ignore[bad-override]
    __table_args__ = (
        CheckConstraint("seq >= 0"),
        CheckConstraint("kind IN ('request', 'response')"),
        CheckConstraint("input_tokens IS NULL OR input_tokens >= 0"),
        CheckConstraint("output_tokens IS NULL OR output_tokens >= 0"),
    )

    session_id: UUID = Field(primary_key=True)
    seq: int = Field(primary_key=True)
    kind: str = Field(sa_type=Text)
    created_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
        )
    )
    message: dict[str, Any] = Field(sa_column=Column(JSON(none_as_null=True), nullable=False))
    message_metadata: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON(none_as_null=True), nullable=False)
    )
    finish_reason: str | None = Field(default=None, sa_type=Text)
    input_tokens: int | None = Field(default=None)
    output_tokens: int | None = Field(default=None)

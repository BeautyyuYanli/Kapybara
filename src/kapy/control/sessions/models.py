"""Session configuration, FIFO inputs and a coalescing cancellation bit.

Configuration does not own runner state. References are checked by services rather
than physical FKs; input and agent rows use the same session identifier.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, BigInteger, CheckConstraint, Column, DateTime, Identity, Index, Text
from sqlmodel import Field

from kapy.control.database import ControlTable
from kapy.control.types import utc_now


class SessionRow(ControlTable, table=True):
    __tablename__ = "sessions"  # pyrefly: ignore[bad-override]
    __table_args__ = (
        Index("ix_sessions_provider_model", "provider_id", "model_name"),
        CheckConstraint("compaction_threshold_tokens > 0"),
        CheckConstraint("compaction_replay_turns >= 0"),
    )

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    title: str = Field(default="", sa_type=Text)
    provider_id: UUID
    model_name: str = Field(sa_type=Text)
    model_settings: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON(none_as_null=True), nullable=False)
    )
    compaction_threshold_tokens: int | None = None
    compaction_replay_turns: int = 10
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class SessionInputRow(ControlTable, table=True):
    __tablename__ = "session_inputs"  # pyrefly: ignore[bad-override]
    __table_args__ = (
        CheckConstraint("channel IN ('steer', 'queued')"),
        Index("session_inputs_order", "session_id", "channel", "id"),
    )

    id: int | None = Field(default=None, sa_column=Column(BigInteger, Identity(), primary_key=True))
    session_id: UUID
    channel: str = Field(sa_type=Text)
    content: Any = Field(sa_column=Column(JSON(none_as_null=True), nullable=False))


class SessionCancelRow(ControlTable, table=True):
    """A row's presence is the signal; consumption deletes it in the runner transaction."""

    __tablename__ = "session_cancels"  # pyrefly: ignore[bad-override]
    session_id: UUID = Field(primary_key=True)

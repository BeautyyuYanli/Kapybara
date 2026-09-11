"""Latest execution position and append-only native messages, without physical FKs."""

from datetime import datetime
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, MetaData, Text, func
from sqlmodel import Field, SQLModel

agent_metadata = MetaData()


class AgentStateRow(SQLModel, table=True):
    metadata: ClassVar[MetaData] = agent_metadata
    __tablename__ = "agent_states"  # pyrefly: ignore[bad-override]
    __table_args__ = (CheckConstraint("next_step IN ('model_request', 'handle_response', 'done')"),)

    session_id: UUID = Field(primary_key=True)
    next_step: str = Field(default="done", sa_type=Text)
    lock_token: UUID | None = None
    heartbeat_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
        )
    )
    updated_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
        )
    )


class AgentHistoryRow(SQLModel, table=True):
    metadata: ClassVar[MetaData] = agent_metadata
    __tablename__ = "agent_history"  # pyrefly: ignore[bad-override]
    __table_args__ = (
        CheckConstraint("seq >= 0"),
        CheckConstraint("kind IN ('request', 'response')"),
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

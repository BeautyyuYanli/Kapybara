"""Session configuration, lifecycle and durable runner snapshot.

Sessions store the latest opaque runner snapshot; the
runner owns the encoding. Fixed native layer Config and mutable internal State
live in separate JSON columns. Tools mutate their run-local State; the executing
Activity's final snapshot saves all States and messages together. CLOSING cleanup saves individual
layer progress. CREATING stores fixed Config and empty States before enter;
publication saves all initial States with READY, and abandoned creation keeps the
States empty through cleanup. Model references may dangle; no physical FKs tie
history retention to configuration.
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Enum,
    Index,
    Text,
)
from sqlmodel import Field

from kapy.control.database import ControlTable
from kapy.control.types import utc_now
from kapy.lifecycle import LifecycleStatus


class SessionRow(ControlTable, table=True):
    __tablename__ = "sessions"  # pyrefly: ignore[bad-override]
    __table_args__ = (Index("ix_sessions_provider_model", "provider_id", "model_name"),)

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    status: LifecycleStatus = Field(
        default=LifecycleStatus.READY,
        sa_column=Column(
            Enum(
                LifecycleStatus, native_enum=False, values_callable=lambda e: [v.value for v in e]
            ),
            nullable=False,
            server_default="ready",
        ),
    )
    title: str = Field(default="", sa_type=Text)
    provider_id: UUID
    model_name: str = Field(sa_type=Text)
    model_settings: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON(none_as_null=True), nullable=False)
    )
    layers: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON(none_as_null=True), nullable=False, server_default="{}"),
    )
    layer_states: dict[str, Any] = Field(
        default_factory=dict,
        sa_column=Column(JSON(none_as_null=True), nullable=False, server_default="{}"),
    )
    runner_state: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    current_activity_id: UUID | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )

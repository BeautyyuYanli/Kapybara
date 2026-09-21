"""Core-owned JSON bindings; plugin resources themselves never belong in this table."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, Enum, Text
from sqlmodel import Field

from kapy.control.database import ControlTable
from kapy.control.types import utc_now
from kapy.lifecycle import LifecycleStatus


class PluginBindingRow(ControlTable, table=True):
    __tablename__ = "plugin_agent_bindings"  # pyrefly: ignore[bad-override]
    __table_args__ = (CheckConstraint("data_version > 0"),)

    session_id: UUID = Field(primary_key=True)
    plugin_provider: str = Field(primary_key=True, sa_type=Text)
    plugin_name: str = Field(primary_key=True, sa_type=Text)
    data_version: int
    # RootModel[None] is valid config: encode Python None as JSON null, not SQL NULL.
    config: Any = Field(sa_column=Column(JSON(none_as_null=False), nullable=False))
    state: Any = Field(default=None, sa_column=Column(JSON(none_as_null=True), nullable=True))
    revision: UUID = Field(default_factory=uuid4)
    status: LifecycleStatus = Field(
        default=LifecycleStatus.READY,
        sa_column=Column(
            Enum(
                LifecycleStatus, native_enum=False, values_callable=lambda e: [v.value for v in e]
            ),
            nullable=False,
        ),
    )
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )

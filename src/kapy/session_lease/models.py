"""Session ownership, independent of business configuration and runner checkpoints."""

from datetime import datetime
from typing import ClassVar
from uuid import UUID

from sqlalchemy import Column, DateTime, MetaData, func
from sqlmodel import Field, SQLModel

lease_metadata = MetaData()


class SessionLeaseRow(SQLModel, table=True):
    metadata: ClassVar[MetaData] = lease_metadata
    __tablename__ = "session_leases"  # pyrefly: ignore[bad-override]

    session_id: UUID = Field(primary_key=True)
    lock_token: UUID | None = None
    heartbeat_at: datetime = Field(
        sa_column=Column(
            DateTime(timezone=True), nullable=False, server_default=func.clock_timestamp()
        )
    )

"""Pending inputs and a coalescing cancellation bit; no business session lifecycle."""

from typing import Any
from uuid import UUID

from sqlalchemy import JSON, BigInteger, CheckConstraint, Column, Identity, Index, Text
from sqlmodel import Field

from kapy.tmpv2.control.database import ControlTable


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

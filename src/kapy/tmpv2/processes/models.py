"""The service's sole table, with metadata isolated from the rest of the application."""

from datetime import datetime
from typing import ClassVar
from uuid import UUID

from sqlalchemy import CheckConstraint, Index, MetaData, text
from sqlmodel import Field, SQLModel


class ProcessRow(SQLModel, table=True):
    metadata: ClassVar[MetaData] = MetaData()
    __tablename__ = "processes"  # pyrefly: ignore[bad-override] -- SQLModel table convention
    __table_args__ = (
        CheckConstraint("mode IN ('stdio', 'pty')"),
        CheckConstraint("state IN ('starting','running','terminating','exited','failed','lost')"),
        CheckConstraint("resource_state IN ('active','deleting')"),
        CheckConstraint("output_state IN ('collecting','complete','incomplete')"),
        CheckConstraint("(state IN ('exited','failed','lost')) = (finished_at IS NOT NULL)"),
        Index("processes_deleting", "process_id", sqlite_where=text("resource_state = 'deleting'")),
    )

    process_id: UUID = Field(primary_key=True)
    mode: str
    cwd: str
    state: str
    resource_state: str = "active"
    output_state: str = "collecting"
    created_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None

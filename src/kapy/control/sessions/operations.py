"""Durable Activity identity and conditional session ownership.

Every mutation first conditionally writes the session, then the operation in the
same short transaction. The second CAS handles stale subquery snapshots after
concurrent UPDATE waits. Raising OwnershipLost rolls both writes back. No explicit
row locks, lease or transaction crosses remote I/O; terminal results never change.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, Column, DateTime, Enum, Index, Text, func, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import Field, col, select

from kapy.control.database import ControlTable
from kapy.control.types import utc_now
from kapy.lifecycle import LifecycleStatus

from .models import SessionRow
from .types import (
    OperationConflict,
    OperationKind,
    OperationPhase,
    OwnershipLost,
    SessionOperation,
    SessionRecord,
)

ACTIVE = (OperationPhase.PENDING, OperationPhase.RUNNING)
TERMINAL = (OperationPhase.SUCCEEDED, OperationPhase.FAILED)


class SessionOperationRow(ControlTable, table=True):
    """Permanent idempotency ledger; no FK allows independent retained history."""

    __tablename__ = "session_operations"  # pyrefly: ignore[bad-override]
    __table_args__ = (Index("ix_session_operations_session", "session_id", "created_at"),)

    activity_id: UUID = Field(primary_key=True)
    session_id: UUID
    kind: OperationKind = Field(
        sa_column=Column(
            Enum(OperationKind, native_enum=False, values_callable=lambda e: [v.value for v in e]),
            nullable=False,
        )
    )
    phase: OperationPhase = Field(
        default=OperationPhase.PENDING,
        sa_column=Column(
            Enum(OperationPhase, native_enum=False, values_callable=lambda e: [v.value for v in e]),
            nullable=False,
        ),
    )
    request: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    input: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    run_id: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    result: Any = Field(default=None, sa_column=Column(JSON(none_as_null=True), nullable=True))
    error: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    deadline: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))


def public_operation(row: SessionOperationRow) -> SessionOperation:
    return SessionOperation.model_validate(row)


class OperationRepository:
    """Borrow a caller-owned short transaction; never commit or call Temporal."""

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get(self, activity_id: UUID) -> SessionOperationRow:
        row = await self.db.get(SessionOperationRow, activity_id, populate_existing=True)
        if row is None:
            raise LookupError("Session operation does not exist")
        return row

    async def insert(self, row: SessionOperationRow) -> tuple[SessionOperationRow, bool]:
        inserted = (
            await self.db.execute(
                insert(SessionOperationRow)
                .values(**row.model_dump())
                .on_conflict_do_nothing()
                .returning(col(SessionOperationRow.activity_id))
            )
        ).scalar_one_or_none()
        existing = await self.get(row.activity_id)
        if (
            existing.kind != row.kind
            or existing.request != row.request
            or (row.kind != OperationKind.CREATE and existing.session_id != row.session_id)
        ):
            raise OperationConflict("Activity ID belongs to a different session operation")
        return existing, inserted is not None

    async def guard(
        self,
        activity_id: UUID,
        statuses: tuple[LifecycleStatus, ...],
        *,
        phases: tuple[OperationPhase, ...] = (OperationPhase.RUNNING,),
        expired: bool = False,
    ) -> tuple[SessionRow, SessionOperationRow]:
        """Conditionally claim both rows; caller must roll back if ownership is lost."""
        eligible = select(SessionOperationRow.activity_id).where(
            col(SessionOperationRow.activity_id) == activity_id,
            col(SessionOperationRow.phase).in_(phases),
        )
        if not expired:
            eligible = eligible.where(col(SessionOperationRow.deadline) > func.clock_timestamp())
        session = (
            await self.db.execute(
                update(SessionRow)
                .where(
                    col(SessionRow.current_activity_id) == activity_id,
                    col(SessionRow.status).in_(statuses),
                    col(SessionRow.current_activity_id).in_(eligible),
                )
                .values(updated_at=utc_now())
                .returning(SessionRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if session is None:
            raise OwnershipLost("Session operation no longer owns its state")
        statement = update(SessionOperationRow).where(
            col(SessionOperationRow.activity_id) == activity_id,
            col(SessionOperationRow.phase).in_(phases),
        )
        if not expired:
            statement = statement.where(col(SessionOperationRow.deadline) > func.clock_timestamp())
        operation = (
            await self.db.execute(
                statement.values(phase=SessionOperationRow.phase)
                .returning(SessionOperationRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if operation is None:
            raise OwnershipLost("Session operation is terminal or expired")
        return session, operation

    async def claim(
        self, activity_id: UUID, kind: OperationKind, run_id: str
    ) -> SessionOperationRow:
        status = {
            OperationKind.CREATE: LifecycleStatus.CREATING,
            OperationKind.RUN: LifecycleStatus.STARTING,
            OperationKind.CLOSE: LifecycleStatus.CLOSING,
        }[kind]
        session, operation = await self.guard(
            activity_id, (status,), phases=(OperationPhase.PENDING,)
        )
        if operation.kind != kind or operation.run_id not in (None, run_id):
            raise OwnershipLost("Activity execution identity does not match its operation")
        operation.phase, operation.run_id = OperationPhase.RUNNING, run_id
        if kind == OperationKind.RUN:
            session.status = LifecycleStatus.RUNNING
        await self.db.flush()
        return operation

    async def bind_run_id(self, activity_id: UUID, run_id: str) -> None:
        """Bind only the current active operation; never replace an actual execution ID."""
        await self.db.execute(
            update(SessionOperationRow)
            .where(
                col(SessionOperationRow.activity_id) == activity_id,
                col(SessionOperationRow.phase).in_(ACTIVE),
                or_(
                    col(SessionOperationRow.run_id).is_(None),
                    col(SessionOperationRow.run_id) == run_id,
                ),
                col(SessionOperationRow.activity_id).in_(select(SessionRow.current_activity_id)),
            )
            .values(run_id=run_id)
        )

    async def fail(self, activity_id: UUID, error: str, *, expired: bool = False) -> None:
        session, operation = await self.guard(
            activity_id,
            (
                LifecycleStatus.CREATING,
                LifecycleStatus.STARTING,
                LifecycleStatus.RUNNING,
                LifecycleStatus.CLOSING,
            ),
            phases=ACTIVE,
            expired=expired,
        )
        session.status = LifecycleStatus.CLOSING
        operation.phase, operation.error = OperationPhase.FAILED, error
        await self.db.flush()

    async def complete(
        self,
        activity_id: UUID,
        status: LifecycleStatus,
        result: Any,
        *,
        runner_state: str | None = None,
        layer_states: dict[str, Any] | None = None,
        failed: bool = False,
        error: str | None = None,
    ) -> SessionRow:
        session, operation = await self.guard(
            activity_id,
            (
                LifecycleStatus.CREATING,
                LifecycleStatus.RUNNING,
                LifecycleStatus.CLOSING,
            ),
        )
        if layer_states is not None:
            if layer_states.keys() != session.layers.keys():
                raise ValueError("Saved layer states must match the configured layer names")
            session.layer_states = layer_states
        if runner_state is not None:
            session.runner_state = runner_state
        session.status = status
        operation.phase = OperationPhase.FAILED if failed else OperationPhase.SUCCEEDED
        operation.result = (
            SessionRecord.model_validate(session).model_dump(mode="json")
            if operation.kind != OperationKind.RUN and not failed
            else result
        )
        operation.error = error
        await self.db.flush()
        return session

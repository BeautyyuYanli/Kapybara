"""Business state and current writer, in caller-owned short transactions.

Temporal owns execution state. Every progress write locks the session through a
conditional UPDATE of its full native execution identity. Configuration edits and
Worker admission compete on that same row; no lock or transaction spans I/O.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import func, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from kapy.control.types import utc_now
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.pagination import Page, paginate

from .models import SessionRow
from .types import OwnershipLost, SessionAlreadyExists, SessionBusy, SessionRecord


class SessionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _session(self, session_id: UUID) -> SessionRow:
        row = await self._db.get(SessionRow, session_id, populate_existing=True)
        if row is None:
            raise LookupError(f"Session {session_id} does not exist")
        return row

    async def get_session(self, session_id: UUID) -> SessionRecord:
        return SessionRecord.model_validate(await self._session(session_id))

    async def read_runner_state(self, session_id: UUID) -> str | None:
        return (await self._session(session_id)).runner_state

    async def list_sessions(
        self, *, provider_id: UUID | None, model_name: str | None, offset: int, limit: int
    ) -> Page[SessionRecord]:
        statement = select(SessionRow)
        if provider_id is not None:
            statement = statement.where(col(SessionRow.provider_id) == provider_id)
        if model_name is not None:
            statement = statement.where(col(SessionRow.model_name) == model_name)
        statement = statement.order_by(col(SessionRow.created_at), col(SessionRow.id)).offset(
            offset
        )
        return await paginate(
            self._db,
            statement,
            limit=limit,
            decode_rows=lambda rows: [SessionRecord.model_validate(row) for row in rows],
        )

    async def update_session(self, session_id: UUID, values: dict[str, Any]) -> SessionRecord:
        """Only an unowned READY session can edit title or execution configuration."""
        row = (
            await self._db.execute(
                update(SessionRow)
                .where(
                    col(SessionRow.id) == session_id,
                    col(SessionRow.status) == LifecycleStatus.READY,
                    col(SessionRow.current_activity_id).is_(None),
                    col(SessionRow.current_activity_run_id).is_(None),
                )
                .values(**(values | {"updated_at": utc_now()}))
                .returning(SessionRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            existing = await self._session(session_id)
            if existing.current_activity_id is not None:
                raise SessionBusy(f"Session {session_id} is occupied")
            raise LifecycleError(f"Session {session_id} is {existing.status}")
        return SessionRecord.model_validate(row)

    async def create(self, row: SessionRow) -> None:
        inserted = (
            await self._db.execute(
                insert(SessionRow)
                .values(**row.model_dump())
                .on_conflict_do_nothing()
                .returning(col(SessionRow.id))
            )
        ).scalar_one_or_none()
        if inserted is None:
            raise SessionAlreadyExists(row.id)

    async def claim_run(self, session_id: UUID, activity_id: UUID, run_id: str) -> SessionRow:
        row = await self.claim_close_or_run(session_id, activity_id, run_id, close=False)
        if row is None:
            existing = await self._session(session_id)
            if existing.status != LifecycleStatus.READY:
                raise LifecycleError(f"Session {session_id} is {existing.status}")
            raise SessionBusy(f"Session {session_id} is occupied")
        return row

    async def claim_close_or_run(
        self,
        session_id: UUID,
        activity_id: UUID,
        run_id: str,
        *,
        close: bool,
        previous: tuple[UUID, str, LifecycleStatus] | None = None,
    ) -> SessionRow | None:
        """Claim empty ownership, or CAS a terminal execution confirmed by close.

        A takeover replaces both identity fields directly. The caller queries
        Temporal outside this transaction and supplies the exact observed owner.
        """
        statement = update(SessionRow).where(col(SessionRow.id) == session_id)
        if previous is None:
            statement = statement.where(
                col(SessionRow.current_activity_id).is_(None),
                col(SessionRow.current_activity_run_id).is_(None),
                col(SessionRow.status).in_(
                    (LifecycleStatus.READY, LifecycleStatus.CLOSING)
                    if close
                    else (LifecycleStatus.READY,)
                ),
            )
        else:
            if not close:
                raise ValueError("Only close can replace ownership")
            statement = statement.where(
                col(SessionRow.current_activity_id) == previous[0],
                col(SessionRow.current_activity_run_id) == previous[1],
                col(SessionRow.status) == previous[2],
            )
        values: dict[str, Any] = {
            "current_activity_id": activity_id,
            "current_activity_run_id": run_id,
            "updated_at": func.clock_timestamp(),
        }
        if close:
            values["status"] = LifecycleStatus.CLOSING
        return (
            await self._db.execute(
                statement.values(**values)
                .returning(SessionRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def guard(
        self,
        session_id: UUID,
        activity_id: UUID,
        run_id: str,
        statuses: tuple[LifecycleStatus, ...],
    ) -> SessionRow:
        """Fence writes through transaction completion; never infer Temporal status."""
        row = (
            await self._db.execute(
                update(SessionRow)
                .where(
                    col(SessionRow.id) == session_id,
                    col(SessionRow.current_activity_id) == activity_id,
                    col(SessionRow.current_activity_run_id) == run_id,
                    col(SessionRow.status).in_(statuses),
                )
                .values(updated_at=func.clock_timestamp())
                .returning(SessionRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            raise OwnershipLost("Native execution no longer owns this session")
        return row

    async def complete(
        self,
        session_id: UUID,
        activity_id: UUID,
        run_id: str,
        statuses: tuple[LifecycleStatus, ...],
        status: LifecycleStatus,
        *,
        runner_state: str | None = None,
        layer_states: dict[str, Any] | None = None,
    ) -> SessionRecord:
        row = await self.guard(session_id, activity_id, run_id, statuses)
        if layer_states is not None:
            if layer_states.keys() != row.layers.keys():
                raise ValueError("Saved layer states must match configured layers")
            row.layer_states = layer_states
        if runner_state is not None:
            row.runner_state = runner_state
        row.status = status
        row.current_activity_id = row.current_activity_run_id = None
        await self._db.flush()
        return SessionRecord.model_validate(row)

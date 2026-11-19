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

    async def get_status(self, session_id: UUID) -> LifecycleStatus | None:
        return await self._db.scalar(
            select(col(SessionRow.status)).where(col(SessionRow.id) == session_id)
        )

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
        row = await self._update_ready(session_id, None, **(values | {"updated_at": utc_now()}))
        if row is None:
            existing = await self._session(session_id)
            if existing.status == LifecycleStatus.CLOSED:
                raise LifecycleError(f"Session {session_id} is closed")
            raise SessionBusy(f"Session {session_id} is occupied")
        return SessionRecord.model_validate(row)

    async def create(self, row: SessionRow) -> None:
        inserted = (
            await self._db.execute(
                insert(SessionRow)
                .values(**row.model_dump())
                .on_conflict_do_nothing(index_elements=[col(SessionRow.id)])
                .returning(col(SessionRow.id))
            )
        ).scalar_one_or_none()
        if inserted is None:
            raise SessionAlreadyExists(row.id)

    async def claim_run(
        self,
        session_id: UUID,
        activity_id: UUID,
        run_id: str,
        *,
        previous: tuple[UUID, str] | None = None,
    ) -> SessionRow | None:
        """Claim an idle session or replace an exact writer confirmed by the caller."""
        return await self._update_ready(
            session_id, previous, current_activity_id=activity_id, current_activity_run_id=run_id
        )

    async def close_session(
        self, session_id: UUID, *, previous: tuple[UUID, str] | None = None
    ) -> SessionRecord | None:
        """Close an idle session or clear the exact writer confirmed by the caller."""
        row = await self._update_ready(
            session_id,
            previous,
            status=LifecycleStatus.CLOSED,
            current_activity_id=None,
            current_activity_run_id=None,
        )
        return SessionRecord.model_validate(row) if row is not None else None

    async def _update_ready(
        self, session_id: UUID, writer: tuple[UUID, str] | None, /, **values: Any
    ) -> SessionRow | None:
        """Fence mutations on READY and the full expected writer pair, including idle."""
        activity_id, run_id = writer if writer is not None else (None, None)
        return (
            await self._db.execute(
                update(SessionRow)
                .where(
                    col(SessionRow.id) == session_id,
                    col(SessionRow.status) == LifecycleStatus.READY,
                    col(SessionRow.current_activity_id) == activity_id,
                    col(SessionRow.current_activity_run_id) == run_id,
                )
                .values(**({"updated_at": func.clock_timestamp()} | values))
                .returning(SessionRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()

    async def guard(self, session_id: UUID, activity_id: UUID, run_id: str) -> SessionRow:
        """Fence writes through transaction completion; never infer Temporal status."""
        row = await self._update_ready(session_id, (activity_id, run_id))
        if row is None:
            raise OwnershipLost("Native execution no longer owns this session")
        return row

    async def complete(
        self,
        session_id: UUID,
        activity_id: UUID,
        run_id: str,
        *,
        runner_state: str | None = None,
        layer_states: dict[str, Any] | None = None,
    ) -> SessionRecord:
        """Release the exact writer, with an optional final snapshot and State."""
        row = await self.guard(session_id, activity_id, run_id)
        if layer_states is not None:
            if layer_states.keys() != row.layers.keys():
                raise ValueError("Saved layer states must match configured layers")
            row.layer_states = layer_states
        if runner_state is not None:
            row.runner_state = runner_state
        row.current_activity_id = row.current_activity_run_id = None
        await self._db.flush()
        return SessionRecord.model_validate(row)

"""Session reads and READY configuration updates borrow short transactions.

OperationRepository owns execution and scope progress. No explicit row lock is
needed: configuration updates race against admission using the READY predicate.
"""

from typing import Any
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from kapy.control.types import utc_now
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.pagination import Page, paginate

from .models import SessionRow
from .types import SessionRecord


class SessionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _session(self, session_id: UUID) -> SessionRow:
        row = await self._db.get(SessionRow, session_id)
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
        """Only READY can edit title or execution configuration."""
        row = (
            await self._db.execute(
                update(SessionRow)
                .where(
                    col(SessionRow.id) == session_id,
                    col(SessionRow.status) == LifecycleStatus.READY,
                )
                .values(**(values | {"updated_at": utc_now()}))
                .returning(SessionRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            existing = await self._session(session_id)
            raise LifecycleError(f"Session {session_id} is {existing.status}")
        return SessionRecord.model_validate(row)

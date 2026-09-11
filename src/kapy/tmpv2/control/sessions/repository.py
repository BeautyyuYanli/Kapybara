"""Session/configuration operations borrow a transaction and never inspect agent state."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from kapy.tmpv2.agent_runner.types import UserInput
from kapy.tmpv2.control.types import utc_now

from .models import SessionCancelRow, SessionInputRow, SessionRow
from .types import CreateSession, InputChannel, SessionInput, SessionRecord

_input_adapter = TypeAdapter(UserInput)


class SessionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _session(self, session_id: UUID) -> SessionRow:
        row = await self._db.get(SessionRow, session_id)
        if row is None:
            raise LookupError(f"Session {session_id} does not exist")
        return row

    async def create_session(self, data: CreateSession) -> SessionRecord:
        row = SessionRow(**data.model_dump())
        self._db.add(row)
        await self._db.flush()
        return SessionRecord.model_validate(row)

    async def get_session(self, session_id: UUID) -> SessionRecord:
        return SessionRecord.model_validate(await self._session(session_id))

    async def list_sessions(
        self, *, provider_id: UUID | None, model_name: str | None, offset: int, limit: int
    ) -> tuple[SessionRecord, ...]:
        statement = select(SessionRow)
        if provider_id is not None:
            statement = statement.where(col(SessionRow.provider_id) == provider_id)
        if model_name is not None:
            statement = statement.where(col(SessionRow.model_name) == model_name)
        statement = (
            statement.order_by(col(SessionRow.created_at), col(SessionRow.id))
            .offset(offset)
            .limit(limit)
        )
        return tuple(
            SessionRecord.model_validate(row)
            for row in (await self._db.execute(statement)).scalars()
        )

    async def update_session(self, session_id: UUID, values: dict[str, Any]) -> SessionRecord:
        row = await self._session(session_id)
        row.sqlmodel_update(values | {"updated_at": utc_now()})
        await self._db.flush()
        return SessionRecord.model_validate(row)

    async def uses_model(self, provider_id: UUID, model_name: str) -> bool:
        statement = select(
            select(SessionRow.id)
            .where(
                col(SessionRow.provider_id) == provider_id,
                col(SessionRow.model_name) == model_name,
            )
            .exists()
        )
        return (await self._db.execute(statement)).scalar_one()

    async def uses_provider(self, provider_id: UUID) -> bool:
        statement = select(
            select(SessionRow.id).where(col(SessionRow.provider_id) == provider_id).exists()
        )
        return (await self._db.execute(statement)).scalar_one()

    async def enqueue_input(
        self, session_id: UUID, channel: InputChannel, content: UserInput
    ) -> SessionInput:
        content = _input_adapter.validate_python(content)
        row = SessionInputRow(
            session_id=session_id,
            channel=channel,
            content=_input_adapter.dump_python(content, mode="json"),
        )
        self._db.add(row)
        await self._db.flush()
        assert row.id is not None
        return SessionInput(row.id, _input_adapter.validate_python(row.content))

    async def read_inputs(
        self, session_id: UUID, channel: InputChannel
    ) -> tuple[SessionInput, ...]:
        statement = (
            select(SessionInputRow)
            .where(
                col(SessionInputRow.session_id) == session_id,
                col(SessionInputRow.channel) == channel,
            )
            .order_by(col(SessionInputRow.id))
        )
        rows = (await self._db.execute(statement)).scalars()
        return tuple(
            SessionInput(row.id, _input_adapter.validate_python(row.content))
            for row in rows
            if row.id is not None
        )

    async def consume_inputs(
        self, session_id: UUID, channel: InputChannel, *, ids: Sequence[int]
    ) -> None:
        if ids:
            await self._db.execute(
                delete(SessionInputRow).where(
                    col(SessionInputRow.session_id) == session_id,
                    col(SessionInputRow.channel) == channel,
                    col(SessionInputRow.id).in_(ids),
                )
            )

    async def set_cancel(self, session_id: UUID) -> None:
        await self._db.execute(
            insert(SessionCancelRow).values(session_id=session_id).on_conflict_do_nothing()
        )

    async def read_cancel(self, session_id: UUID) -> bool:
        statement = select(col(SessionCancelRow.session_id)).where(
            col(SessionCancelRow.session_id) == session_id
        )
        return (await self._db.execute(statement)).scalar_one_or_none() is not None

    async def consume_cancel(self, session_id: UUID) -> bool:
        statement = (
            delete(SessionCancelRow)
            .where(col(SessionCancelRow.session_id) == session_id)
            .returning(col(SessionCancelRow.session_id))
        )
        return (await self._db.execute(statement)).scalar_one_or_none() is not None

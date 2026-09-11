"""Session input operations borrow the caller's transaction and never inspect agent state."""

from collections.abc import Sequence
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from kapy.tmpv2.agent_runner.types import UserInput

from .models import SessionCancelRow, SessionInputRow
from .types import InputChannel, SessionInput

_input_adapter = TypeAdapter(UserInput)


class SessionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

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

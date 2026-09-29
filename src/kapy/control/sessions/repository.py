"""Session/configuration operations borrow a transaction and never inspect agent state."""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from kapy.agent_runner.types import UserInput
from kapy.control.types import utc_now
from kapy.pagination import Page, paginate

from .models import SessionCancelRow, SessionInputRow, SessionRow
from .types import CreateSession, InputChannel, SessionInput, SessionRecord

_input_adapter = TypeAdapter(UserInput)


async def lock_session(db: AsyncSession, session_id: UUID) -> SessionRow:
    """Lock and refresh the row even when this Session already loaded an older version."""
    row = (
        await db.execute(
            select(SessionRow)
            .where(col(SessionRow.id) == session_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"Session {session_id} does not exist")
    return row


class SessionRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _session(self, session_id: UUID) -> SessionRow:
        row = await self._db.get(SessionRow, session_id)
        if row is None:
            raise LookupError(f"Session {session_id} does not exist")
        return row

    async def create_session(self, data: CreateSession) -> SessionRecord:
        row = SessionRow(**data.model_dump(exclude={"plugins"}))
        self._db.add(row)
        await self._db.flush()
        return SessionRecord.model_validate(row)

    async def get_session(self, session_id: UUID) -> SessionRecord:
        return SessionRecord.model_validate(await self._session(session_id))

    async def read_runner_state(self, session_id: UUID) -> tuple[str | None, int]:
        """Read one opaque snapshot/version pair; missing sessions raise LookupError."""
        row = (
            await self._db.execute(
                select(SessionRow.runner_state, SessionRow.runner_state_version).where(
                    col(SessionRow.id) == session_id
                )
            )
        ).one_or_none()
        if row is None:
            raise LookupError(f"Session {session_id} does not exist")
        return row.runner_state, row.runner_state_version

    async def save_runner_state(
        self, session_id: UUID, *, expected_version: int, runner_state: str
    ) -> None:
        """Replace opaque state under a row lock, borrowing the caller's transaction.

        Matching expected_version replaces state and increments the version. A
        retry succeeds unchanged only at expected_version + 1 with an identical
        string. All other version/string mismatches raise ValueError, including
        retries after further advancement. Missing sessions raise LookupError.
        The runner owns serialization and no state is parsed.
        """
        if expected_version < 0:
            raise ValueError("expected_version must be nonnegative")
        row = await lock_session(self._db, session_id)
        if row.runner_state_version == expected_version:
            row.runner_state = runner_state
            row.runner_state_version = expected_version + 1
            row.updated_at = utc_now()
            await self._db.flush()
        elif not (
            row.runner_state_version == expected_version + 1 and row.runner_state == runner_state
        ):
            raise ValueError(f"Session {session_id} runner state version conflicts")

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
        row = await self._session(session_id)
        if "context_plugin" in values:
            if values["context_plugin"]["name"] != row.context_plugin["name"]:
                raise ValueError("context_plugin.name cannot change after session creation")
        row.sqlmodel_update(values | {"updated_at": utc_now()})
        await self._db.flush()
        return SessionRecord.model_validate(row)

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

    async def delete_input(self, session_id: UUID, input_id: int) -> bool:
        statement = (
            delete(SessionInputRow)
            .where(
                col(SessionInputRow.session_id) == session_id, col(SessionInputRow.id) == input_id
            )
            .returning(col(SessionInputRow.id))
        )
        return (await self._db.execute(statement)).scalar_one_or_none() is not None

    async def consume_inputs(
        self, session_id: UUID, channel: InputChannel, *, ids: Sequence[int]
    ) -> tuple[SessionInput, ...]:
        """Return only rows actually removed, in FIFO order, in the borrowed transaction."""
        if not ids:
            return ()
        statement = (
            delete(SessionInputRow)
            .where(
                col(SessionInputRow.session_id) == session_id,
                col(SessionInputRow.channel) == channel,
                col(SessionInputRow.id).in_(ids),
            )
            .returning(col(SessionInputRow.id), col(SessionInputRow.content))
        )
        rows = (await self._db.execute(statement)).all()
        return tuple(
            SessionInput(row.id, _input_adapter.validate_python(row.content))
            for row in sorted(rows, key=lambda row: row.id)
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

"""Short, independent async transactions. The manager owns execution, never ORM sessions."""

from datetime import UTC, datetime
from typing import cast
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import col, select

from .models import ProcessRow
from .types import (
    OutputState,
    ProcessError,
    ProcessMode,
    ProcessPage,
    ProcessState,
    ProcessStatus,
    ResourceState,
)

TERMINAL = {"exited", "failed", "lost"}


def _utc(value: datetime | None) -> datetime | None:
    # SQLite discards the timezone; all writes in this service are UTC.
    return None if value is None else value.replace(tzinfo=UTC)


def status(row: ProcessRow) -> ProcessStatus:
    return ProcessStatus(
        process_id=row.process_id,
        mode=cast(ProcessMode, row.mode),
        cwd=row.cwd,
        state=cast(ProcessState, row.state),
        resource_state=cast(ResourceState, row.resource_state),
        output_state=cast(OutputState, row.output_state),
        created_at=row.created_at.replace(tzinfo=UTC),
        finished_at=_utc(row.finished_at),
        exit_code=row.exit_code,
    )


class ProcessRepository:
    """Borrow an engine; each operation owns its session and commits only database work."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def recover(self) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(ProcessRow)
                .where(
                    col(ProcessRow.state).not_in(TERMINAL),
                )
                .values(
                    state="lost",
                    finished_at=datetime.now(UTC),
                )
            )
            await session.execute(
                update(ProcessRow)
                .where(
                    col(ProcessRow.output_state) == "collecting",
                )
                .values(output_state="incomplete")
            )

    async def get(self, process_id: UUID) -> ProcessRow:
        async with self._sessions() as session:
            row = await session.get(ProcessRow, process_id)
            if row is None:
                raise ProcessError("not_found", "Process not found")
            return row

    async def add(self, row: ProcessRow) -> None:
        async with self._sessions.begin() as session:
            session.add(row)

    async def change(self, process_id: UUID, **values: object) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(ProcessRow)
                .where(
                    col(ProcessRow.process_id) == process_id,
                )
                .values(**values)
            )

    async def mark_terminating(self, process_id: UUID) -> None:
        async with self._sessions.begin() as session:
            await session.execute(
                update(ProcessRow)
                .where(
                    col(ProcessRow.process_id) == process_id,
                    col(ProcessRow.state).not_in(TERMINAL),
                )
                .values(state="terminating")
            )

    async def list(self, after: UUID | None, limit: int) -> ProcessPage:
        query = select(ProcessRow).order_by(col(ProcessRow.process_id)).limit(limit + 1)
        if after is not None:
            query = query.where(col(ProcessRow.process_id) > after)
        async with self._sessions() as session:
            rows = (await session.execute(query)).scalars().all()
        return ProcessPage(
            items=tuple(status(row) for row in rows[:limit]),
            next_after=rows[limit - 1].process_id if len(rows) > limit else None,
        )

    async def deleting(self, limit: int) -> tuple[UUID, ...]:
        async with self._sessions() as session:
            rows = (
                (
                    await session.execute(
                        select(ProcessRow.process_id)
                        .where(
                            col(ProcessRow.resource_state) == "deleting",
                        )
                        .order_by(col(ProcessRow.process_id))
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        return tuple(rows)

    async def delete(self, process_id: UUID) -> int:
        async with self._sessions.begin() as session:
            result = await session.execute(
                delete(ProcessRow)
                .where(
                    col(ProcessRow.process_id) == process_id,
                    col(ProcessRow.resource_state) == "deleting",
                )
                .returning(col(ProcessRow.process_id))
            )
            return len(result.all())

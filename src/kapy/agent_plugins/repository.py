"""Borrowed short transactions; always lock session before binding for mutations.

The session row serializes closing against resource registration and input intake.
No plugin callback, migration or custom validator executes inside these transactions.
UUID CAS orders JSON replacements, not external side effects or resource creation.
"""

from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

if TYPE_CHECKING:
    from kapy.control.sessions.models import SessionRow
from kapy.control.types import utc_now
from kapy.lifecycle import LifecycleError, LifecycleStatus

from .contracts import BindingRecord, PluginData, StateConflict
from .models import PluginBindingRow

type Operation = Literal["execution", "close"]


async def lock_session(db: AsyncSession, session_id: UUID) -> SessionRow:
    from kapy.control.sessions.models import SessionRow

    row = (
        await db.execute(
            select(SessionRow).where(col(SessionRow.id) == session_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise LookupError(f"Session {session_id} does not exist")
    return row


class BindingRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def list(self, session_id: UUID) -> tuple[BindingRecord, ...]:
        rows = (
            await self.db.execute(
                select(PluginBindingRow)
                .where(col(PluginBindingRow.session_id) == session_id)
                .order_by(col(PluginBindingRow.plugin_provider), col(PluginBindingRow.plugin_name))
            )
        ).scalars()
        return tuple(BindingRecord.model_validate(row) for row in rows)

    async def get(self, session_id: UUID, provider: str, name: str) -> PluginBindingRow:
        row = await self.db.get(PluginBindingRow, (session_id, provider, name))
        if row is None:
            raise LookupError(f"Plugin binding {provider}.{name} does not exist")
        return row

    async def allowed(
        self, session_id: UUID, provider: str, name: str, operation: Operation
    ) -> PluginBindingRow:
        session = await lock_session(self.db, session_id)
        row = await self.get(session_id, provider, name)
        expected = LifecycleStatus.READY if operation == "execution" else LifecycleStatus.CLOSING
        if session.status != expected or (operation == "close" and row.status != expected):
            raise LifecycleError(
                f"Plugin {provider}.{name} does not allow {operation} in {session.status}"
            )
        return row

    async def replace(
        self, row: PluginBindingRow, *, version: int, revision: UUID, data: PluginData, target: int
    ) -> BindingRecord:
        if row.data_version != version or row.revision != revision:
            raise StateConflict("Plugin state changed")
        row.config, row.state = data.config, data.state
        row.data_version, row.revision, row.updated_at = target, uuid4(), utc_now()
        await self.db.flush()
        return BindingRecord.model_validate(row)

"""Borrowed short transactions for plugin bindings.

Execution and close accesses require the host to lock its lease first. Ordinary
observation, including list_bindings, does not acquire a lease. Lease fencing
serializes mutations, including same-owner parallel state writes.
No plugin callback, migration or custom validator executes inside these transactions.
UUID CAS orders JSON replacements, not external side effects or resource creation.
"""

from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from kapy.control.types import utc_now

from .contracts import BindingRecord, PluginData, StateConflict
from .models import PluginBindingRow


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

    async def replace(
        self, row: PluginBindingRow, *, version: int, revision: UUID, data: PluginData, target: int
    ) -> BindingRecord:
        if row.data_version != version or row.revision != revision:
            raise StateConflict("Plugin state changed")
        row.config, row.state = data.config, data.state
        row.data_version, row.revision, row.updated_at = target, uuid4(), utc_now()
        await self.db.flush()
        return BindingRecord.model_validate(row)

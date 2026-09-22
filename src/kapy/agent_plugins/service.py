"""Plugin host borrowing the admitted operation's lease for all runtime transactions.

Callers own lifecycle admission, the lease, and plugin child-task lifetimes. Stores
are revoked on exit. Fencing protects state, not external I/O or resource creation;
plugins still own registration compensation and orphan reconciliation.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from contextvars import ContextVar
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kapy.control.types import utc_now
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.session_lease import SessionLease

from .contracts import (
    BindingRecord,
    PluginBinding,
    PluginData,
    PluginOperationError,
    PluginSpec,
    SessionContext,
    VersionedState,
)
from .models import PluginBindingRow
from .registry import PluginDefinition, PluginRegistry, encode_model, json_copy, validate_model
from .repository import BindingRepository

_read_only = ContextVar("plugin_state_read_only", default=False)


class ScopedStateStore:
    """Host-fixed identity/schema and borrowed lease, revoked after plugin cleanup.

    Each read/write fences its own short transaction. Validation runs outside it;
    state mutations are never automatically retried or implicitly saved.
    """

    def __init__(
        self,
        service: AgentPluginService,
        session_id: UUID,
        definition: PluginDefinition,
        lease: SessionLease,
    ) -> None:
        self.service, self.session_id, self.definition, self.lease = (
            service,
            session_id,
            definition,
            lease,
        )
        self.active = True

    def check_active(self) -> None:
        if not self.active:
            raise LifecycleError("Plugin context has expired")
        self.lease.check()

    async def read(self) -> VersionedState[BaseModel]:
        self.check_active()
        async with self.service.session_factory.begin() as db:
            await self.lease.lock_owned(db)
            row = await BindingRepository(db).get(
                self.session_id, self.definition.plugin_provider, self.definition.plugin_name
            )
            revision, value = row.revision, json_copy(row.state)
        parsed = None if value is None else validate_model(self.definition.state_type, value)[0]
        return VersionedState(revision, parsed)

    async def replace(
        self, value: BaseModel, *, expected_revision: UUID
    ) -> VersionedState[BaseModel]:
        self.check_active()
        if _read_only.get():
            raise LifecycleError("Plugin instructions cannot write state")
        if not isinstance(expected_revision, UUID):
            raise ValueError("expected_revision must be UUID")
        if not isinstance(value, self.definition.state_type):
            raise ValueError("State must use the registered state type")
        parsed, raw = validate_model(self.definition.state_type, encode_model(value))
        async with self.service.session_factory.begin() as db:
            await self.lease.lock_owned(db)
            row = await BindingRepository(db).get(
                self.session_id, self.definition.plugin_provider, self.definition.plugin_name
            )
            saved = await BindingRepository(db).replace(
                row,
                version=self.definition.data_version,
                revision=expected_revision,
                data=PluginData(row.config, raw),
                target=self.definition.data_version,
            )
        return VersionedState(saved.revision, parsed)


class AgentPluginService:
    """Borrow registry and session factory; never retain session/plugin instances."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], registry: PluginRegistry
    ) -> None:
        self.session_factory, self.registry = session_factory, registry

    def prepare_bindings(
        self, specs: Sequence[PluginSpec]
    ) -> list[tuple[PluginDefinition, PluginData]]:
        """Validate every binding before the session creation transaction starts."""
        result = []
        seen = set()
        for spec in specs:
            key = (spec.plugin_provider, spec.plugin_name)
            if key in seen:
                raise ValueError(f"Duplicate plugin binding {key}")
            seen.add(key)
            definition = self.registry.get(*key)
            _, config = validate_model(definition.config_type, spec.config)
            result.append((definition, PluginData(config, None)))
        return result

    def create_bindings(
        self,
        db: AsyncSession,
        session_id: UUID,
        prepared: Sequence[tuple[PluginDefinition, PluginData]],
    ) -> None:
        """Join the caller's creation transaction, with already-validated plain JSON."""
        for definition, data in prepared:
            db.add(
                PluginBindingRow(
                    session_id=session_id,
                    plugin_provider=definition.plugin_provider,
                    plugin_name=definition.plugin_name,
                    data_version=definition.data_version,
                    config=data.config,
                    state=None,
                )
            )

    async def list_bindings(self, session_id: UUID) -> tuple[BindingRecord, ...]:
        async with self.session_factory.begin() as db:
            return await BindingRepository(db).list(session_id)

    async def _context(
        self, original: BindingRecord, definition: PluginDefinition, lease: SessionLease
    ) -> tuple[SessionContext[Any, Any], ScopedStateStore]:
        config, data = definition.load(
            original.data_version, PluginData(original.config, original.state)
        )
        if original.data_version != definition.data_version:
            async with self.session_factory.begin() as db:
                await lease.lock_owned(db)
                repo = BindingRepository(db)
                row = await repo.get(
                    original.session_id, definition.plugin_provider, definition.plugin_name
                )
                await repo.replace(
                    row,
                    version=original.data_version,
                    revision=original.revision,
                    data=data,
                    target=definition.data_version,
                )
        store = ScopedStateStore(self, original.session_id, definition, lease)
        return SessionContext(
            original.session_id, definition.plugin_provider, definition.plugin_name, config, store
        ), store

    @staticmethod
    def _check_lease(session_id: UUID, lease: SessionLease) -> None:
        if lease.session_id != session_id:
            raise ValueError("Plugin operation requires the target session's lease")
        lease.check()

    @asynccontextmanager
    async def open_execution(
        self, session_id: UUID, *, lease: SessionLease
    ) -> AsyncIterator[list[tuple[str, str, PluginBinding, ScopedStateStore]]]:
        """Enter in stable order, exit in reverse in this same task; no detached work."""
        self._check_lease(session_id, lease)
        async with self.session_factory.begin() as db:
            await lease.lock_owned(db)
            bindings = await BindingRepository(db).list(session_id)
        async with AsyncExitStack() as stack:
            opened = []
            for binding in bindings:
                provider, name = binding.plugin_provider, binding.plugin_name
                try:
                    definition = self.registry.get(provider, name)
                    ctx, store = await self._context(binding, definition, lease)
                    # Registered before the plugin context so cleanup can still use state.
                    stack.callback(setattr, store, "active", False)
                    result = await stack.enter_async_context(
                        definition.plugin_type().open_execution(ctx)
                    )
                    if not isinstance(result, PluginBinding):
                        raise TypeError("open_execution must yield PluginBinding")
                    opened.append((provider, name, result, store))
                except Exception as error:
                    raise PluginOperationError(provider, name, "open execution") from error
            yield opened

    async def close_binding(self, binding: BindingRecord, *, lease: SessionLease) -> None:
        """Close one binding under the admitted closer's lease; retain partial progress."""
        self._check_lease(binding.session_id, lease)
        provider, name = binding.plugin_provider, binding.plugin_name
        try:
            definition = self.registry.get(provider, name)
            ctx, store = await self._context(binding, definition, lease)
            try:
                await definition.plugin_type().close_session(ctx)
            finally:
                store.active = False
            async with self.session_factory.begin() as db:
                await lease.lock_owned(db)
                row = await BindingRepository(db).get(binding.session_id, provider, name)
                row.status = LifecycleStatus.CLOSED
                row.updated_at = utc_now()
        except Exception as error:
            raise PluginOperationError(provider, name, "close session") from error

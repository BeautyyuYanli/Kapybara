"""Generic builtin plugin service; no resource-specific semantics or runner leases.

Every operation owns its short DB transactions. Contexts are revoked on exit;
closing immediately prevents execution-scope state access. External resource I/O
is never atomic with registration, so plugins must support orphan reconciliation.
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

from .contracts import (
    BindingRecord,
    PluginBinding,
    PluginData,
    PluginOperationError,
    PluginSpec,
    SessionContext,
    StateConflict,
    VersionedState,
)
from .models import PluginBindingRow
from .registry import PluginDefinition, PluginRegistry, encode_model, json_copy, validate_model
from .repository import BindingRepository, Operation

_read_only = ContextVar("plugin_state_read_only", default=False)


class ScopedStateStore:
    """Host-fixed identity/schema/purpose; each read or replace owns one transaction.

    State validators run outside transactions. No automatic writeback, nested
    transaction, lease borrowing or retry of user state mutations occurs here.
    """

    def __init__(
        self,
        service: AgentPluginService,
        session_id: UUID,
        definition: PluginDefinition,
        operation: Operation,
    ) -> None:
        self.service, self.session_id, self.definition, self.operation = (
            service,
            session_id,
            definition,
            operation,
        )
        self.active = True

    def check_active(self) -> None:
        if not self.active:
            raise LifecycleError("Plugin context has expired")

    async def _row(self, db: AsyncSession) -> PluginBindingRow:
        self.check_active()
        row = await BindingRepository(db).allowed(
            self.session_id,
            self.definition.plugin_provider,
            self.definition.plugin_name,
            self.operation,
        )
        self.check_active()
        if row.data_version != self.definition.data_version:
            raise LifecycleError("Plugin data format changed; reopen the context")
        return row

    async def check(self) -> None:
        async with self.service.session_factory.begin() as db:
            await self._row(db)

    async def read(self) -> VersionedState[BaseModel]:
        async with self.service.session_factory.begin() as db:
            row = await self._row(db)
            revision, value = row.revision, json_copy(row.state)
        parsed = None if value is None else validate_model(self.definition.state_type, value)[0]
        self.check_active()
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
            row = await self._row(db)
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
        self, session_id: UUID, definition: PluginDefinition, operation: Operation
    ) -> tuple[SessionContext[Any, Any], ScopedStateStore]:
        while True:
            async with self.session_factory.begin() as db:
                row = await BindingRepository(db).allowed(
                    session_id, definition.plugin_provider, definition.plugin_name, operation
                )
                original = BindingRecord.model_validate(row)
            config, data = definition.load(
                original.data_version, PluginData(original.config, original.state)
            )
            if original.data_version == definition.data_version:
                break
            try:
                async with self.session_factory.begin() as db:
                    repo = BindingRepository(db)
                    row = await repo.allowed(
                        session_id, definition.plugin_provider, definition.plugin_name, operation
                    )
                    await repo.replace(
                        row,
                        version=original.data_version,
                        revision=original.revision,
                        data=data,
                        target=definition.data_version,
                    )
                break
            except StateConflict:
                continue
        store = ScopedStateStore(self, session_id, definition, operation)
        # Validation/migration runs outside locks; closing can win during it.
        await store.check()
        return SessionContext(
            session_id, definition.plugin_provider, definition.plugin_name, config, store
        ), store

    @asynccontextmanager
    async def open_execution(
        self, session_id: UUID
    ) -> AsyncIterator[list[tuple[str, str, PluginBinding, ScopedStateStore]]]:
        """Enter in stable order, exit in reverse in this same task; no detached work."""
        bindings = await self.list_bindings(session_id)
        async with AsyncExitStack() as stack:
            opened = []
            for binding in bindings:
                provider, name = binding.plugin_provider, binding.plugin_name
                try:
                    definition = self.registry.get(provider, name)
                    ctx, store = await self._context(session_id, definition, "execution")
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

    async def close_binding(self, binding: BindingRecord) -> None:
        """Close one binding; recognize only completed-lifecycle competition as success."""
        provider, name = binding.plugin_provider, binding.plugin_name
        store = None
        try:
            definition = self.registry.get(provider, name)
            ctx, store = await self._context(binding.session_id, definition, "close")
            try:
                await definition.plugin_type().close_session(ctx)
            finally:
                store.active = False
            async with self.session_factory.begin() as db:
                row = await BindingRepository(db).allowed(
                    binding.session_id, provider, name, "close"
                )
                row.status = LifecycleStatus.CLOSED
                row.updated_at = utc_now()
        except Exception as error:
            # A cleanup failure can be replaced by a lifecycle error in a plugin's
            # finally block. Even a suppressed exception context must propagate;
            # only an unchained lifecycle rejection is a completion competition.
            if (
                isinstance(error, LifecycleError)
                and error.__context__ is None
                and error.__cause__ is None
            ):
                async with self.session_factory.begin() as db:
                    row = await BindingRepository(db).get(binding.session_id, provider, name)
                    if row.status == LifecycleStatus.CLOSED:
                        return
            raise PluginOperationError(provider, name, "close session") from error
        finally:
            if store is not None:
                store.active = False

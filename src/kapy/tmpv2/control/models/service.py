"""Local model catalog with short transactions around SDK discovery and metadata reads.

Provider/model references are application checks, deliberately not physical FKs.
Deleting configuration never deletes an upstream account, model or credential.
"""

from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kapy.tmpv2.control.sessions.repository import SessionRepository
from kapy.tmpv2.pagination import Page, validate_pagination

from .models import ModelRow
from .repository import ModelRepository
from .runtime import (
    build_provider,
    discover_remote_models,
    infer_context_window,
    normalize_model_name,
    prepare_model,
    resolve_classes,
    validate_provider,
    validate_settings,
)
from .types import (
    CreateModel,
    CreateProvider,
    ModelAlreadyExists,
    ModelDiscoveryError,
    ModelRecord,
    ProviderConfig,
    ProviderRecord,
    ResourceInUse,
    UpdateModel,
    UpdateProvider,
)


class ModelService:
    """Owns transaction boundaries, but no resident ORM session or SDK client."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_provider(self, data: CreateProvider) -> ProviderRecord:
        validate_provider(ProviderConfig.model_validate(data.model_dump(exclude={"name"})))
        async with self._session_factory.begin() as db:
            return await ModelRepository(db).create_provider(data)

    async def get_provider(self, provider_id: UUID) -> ProviderRecord:
        async with self._session_factory.begin() as db:
            return await ModelRepository(db).get_provider(provider_id)

    async def list_providers(self, *, offset: int = 0, limit: int = 100) -> Page[ProviderRecord]:
        validate_pagination(offset, limit)
        async with self._session_factory.begin() as db:
            return await ModelRepository(db).list_providers(offset=offset, limit=limit)

    async def update_provider(self, provider_id: UUID, data: UpdateProvider) -> ProviderRecord:
        async with self._session_factory.begin() as db:
            repo = ModelRepository(db)
            current = await repo.get_provider_config(provider_id)
            values = data.model_dump(exclude_unset=True, exclude={"name"})
            validate_provider(ProviderConfig.model_validate(current.model_dump() | values))
            return await repo.update_provider(provider_id, data)

    async def delete_provider(self, provider_id: UUID) -> None:
        async with self._session_factory.begin() as db:
            if await SessionRepository(db).uses_provider(provider_id):
                raise ResourceInUse(f"Provider {provider_id} is used by a session")
            await ModelRepository(db).delete_provider(provider_id)

    async def create_model(self, data: CreateModel) -> ModelRecord:
        async with self._session_factory.begin() as db:
            config = await ModelRepository(db).get_provider_config(data.provider_id)
        provider_cls, model_cls = resolve_classes(config)
        row = prepare_model(data, model_cls)
        if row.context_window is None:
            async with build_provider(provider_cls, config) as provider:
                row.context_window = await infer_context_window(model_cls, row.model_name, provider)
        try:
            async with self._session_factory.begin() as db:
                repo = ModelRepository(db)
                await repo.get_provider(data.provider_id)
                return await repo.create_model(row)
        except IntegrityError as exc:
            # The only conflicting key on this validated, FK-free row is its identity.
            raise ModelAlreadyExists(str((data.provider_id, row.model_name))) from exc

    async def get_model(self, provider_id: UUID, model_name: str) -> ModelRecord:
        async with self._session_factory.begin() as db:
            return await ModelRepository(db).get_model(provider_id, model_name)

    async def list_models(
        self, *, provider_id: UUID | None = None, offset: int = 0, limit: int = 100
    ) -> Page[ModelRecord]:
        validate_pagination(offset, limit)
        async with self._session_factory.begin() as db:
            return await ModelRepository(db).list_models(
                provider_id=provider_id, offset=offset, limit=limit
            )

    async def update_model(
        self, provider_id: UUID, model_name: str, data: UpdateModel
    ) -> ModelRecord:
        async with self._session_factory.begin() as db:
            repo = ModelRepository(db)
            config = await repo.get_provider_config(provider_id)
            _, model_cls = resolve_classes(config)
            values = data.model_dump(exclude_unset=True)
            if data.settings is not None:
                values["settings"] = validate_settings(model_cls, data.settings)
            return await repo.update_model(provider_id, model_name, values)

    async def delete_model(self, provider_id: UUID, model_name: str) -> None:
        async with self._session_factory.begin() as db:
            if await SessionRepository(db).uses_model(provider_id, model_name):
                raise ResourceInUse(f"Model {(provider_id, model_name)} is used by a session")
            await ModelRepository(db).delete_model(provider_id, model_name)

    async def discover_models(self, provider_id: UUID) -> tuple[ModelRecord, ...]:
        """Import missing remote models atomically; preserve all existing local presets.

        Listing and profile inspection happen outside transactions. Unsupported
        listing, remote failures and malformed discovered data raise ModelDiscoveryError.
        An absent provider at the initial configuration read raises LookupError.
        If deleted during discovery, the final existence check instead fails with
        ModelDiscoveryError, preserving the discovery operation's failure boundary.
        """
        async with self._session_factory.begin() as db:
            config = await ModelRepository(db).get_provider_config(provider_id)
        try:
            provider_cls, model_cls = resolve_classes(config)
            async with build_provider(provider_cls, config) as provider:
                remote = {
                    normalize_model_name(model_cls, name): display
                    for name, display in (await discover_remote_models(provider)).items()
                }
                async with self._session_factory.begin() as db:
                    existing = await ModelRepository(db).model_names(provider_id)
                prepared: list[ModelRow] = []
                for name in sorted(remote.keys() - existing):
                    data = CreateModel(provider_id=provider_id, model_name=name, name=remote[name])
                    row = prepare_model(data, model_cls)
                    row.context_window = await infer_context_window(
                        model_cls, row.model_name, provider
                    )
                    prepared.append(row)
            async with self._session_factory.begin() as db:
                repo = ModelRepository(db)
                await repo.get_provider(provider_id)
                return await repo.import_models(provider_id, prepared, sorted(remote))
        except Exception as exc:
            raise ModelDiscoveryError(
                f"Could not discover models for provider {provider_id}"
            ) from exc

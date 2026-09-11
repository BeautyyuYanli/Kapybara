"""Provider/model persistence borrows a short transaction; it never calls the SDK.

Services validate references and own transaction boundaries. Returned records are
loaded values, so callers can close the transaction before external work.
"""

from collections.abc import Sequence
from typing import Any
from uuid import UUID

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from kapy.tmpv2.control.types import utc_now
from kapy.tmpv2.pagination import Page, paginate

from .models import ModelRow, ProviderRow
from .types import CreateProvider, ModelRecord, ProviderConfig, ProviderRecord, UpdateProvider


class ModelRepository:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def _provider(self, provider_id: UUID) -> ProviderRow:
        row = await self._db.get(ProviderRow, provider_id)
        if row is None:
            raise LookupError(f"Provider {provider_id} does not exist")
        return row

    async def _model(self, provider_id: UUID, model_name: str) -> ModelRow:
        row = await self._db.get(ModelRow, (provider_id, model_name))
        if row is None:
            raise LookupError(f"Model {(provider_id, model_name)} does not exist")
        return row

    async def create_provider(self, data: CreateProvider) -> ProviderRecord:
        values = data.model_dump()
        values["api_key"] = data.api_key.get_secret_value()
        row = ProviderRow(**values)
        self._db.add(row)
        await self._db.flush()
        return ProviderRecord.model_validate(row)

    async def get_provider(self, provider_id: UUID) -> ProviderRecord:
        return ProviderRecord.model_validate(await self._provider(provider_id))

    async def get_provider_config(self, provider_id: UUID) -> ProviderConfig:
        return ProviderConfig.model_validate(await self._provider(provider_id))

    async def list_providers(self, *, offset: int, limit: int) -> Page[ProviderRecord]:
        statement = (
            select(ProviderRow)
            .order_by(col(ProviderRow.created_at), col(ProviderRow.id))
            .offset(offset)
        )
        return await paginate(
            self._db,
            statement,
            limit=limit,
            decode_rows=lambda rows: [ProviderRecord.model_validate(row) for row in rows],
        )

    async def update_provider(self, provider_id: UUID, data: UpdateProvider) -> ProviderRecord:
        row = await self._provider(provider_id)
        values = data.model_dump(exclude_unset=True)
        if data.api_key is not None:
            values["api_key"] = data.api_key.get_secret_value()
        row.sqlmodel_update(values | {"updated_at": utc_now()})
        await self._db.flush()
        return ProviderRecord.model_validate(row)

    async def delete_provider(self, provider_id: UUID) -> None:
        await self._db.execute(delete(ModelRow).where(col(ModelRow.provider_id) == provider_id))
        await self._db.execute(delete(ProviderRow).where(col(ProviderRow.id) == provider_id))

    async def create_model(self, row: ModelRow) -> ModelRecord:
        self._db.add(row)
        await self._db.flush()
        return ModelRecord.model_validate(row)

    async def import_models(
        self, provider_id: UUID, rows: Sequence[ModelRow], names: Sequence[str]
    ) -> tuple[ModelRecord, ...]:
        """Insert missing rows and return all discovered models in name order."""
        existing = await self.model_names(provider_id)
        self._db.add_all(row for row in rows if row.model_name not in existing)
        await self._db.flush()
        statement = (
            select(ModelRow)
            .where(col(ModelRow.provider_id) == provider_id, col(ModelRow.model_name).in_(names))
            .order_by(col(ModelRow.model_name))
        )
        return tuple(
            ModelRecord.model_validate(row) for row in (await self._db.execute(statement)).scalars()
        )

    async def get_model(self, provider_id: UUID, model_name: str) -> ModelRecord:
        return ModelRecord.model_validate(await self._model(provider_id, model_name))

    async def model_names(self, provider_id: UUID) -> set[str]:
        statement = select(col(ModelRow.model_name)).where(col(ModelRow.provider_id) == provider_id)
        return set((await self._db.execute(statement)).scalars())

    async def list_models(
        self, *, provider_id: UUID | None, offset: int, limit: int
    ) -> Page[ModelRecord]:
        statement = select(ModelRow)
        if provider_id is not None:
            statement = statement.where(col(ModelRow.provider_id) == provider_id)
        statement = statement.order_by(
            col(ModelRow.created_at), col(ModelRow.provider_id), col(ModelRow.model_name)
        ).offset(offset)
        return await paginate(
            self._db,
            statement,
            limit=limit,
            decode_rows=lambda rows: [ModelRecord.model_validate(row) for row in rows],
        )

    async def update_model(
        self, provider_id: UUID, model_name: str, values: dict[str, Any]
    ) -> ModelRecord:
        row = await self._model(provider_id, model_name)
        row.sqlmodel_update(values | {"updated_at": utc_now()})
        await self._db.flush()
        return ModelRecord.model_validate(row)

    async def delete_model(self, provider_id: UUID, model_name: str) -> None:
        await self._db.execute(
            delete(ModelRow).where(
                col(ModelRow.provider_id) == provider_id,
                col(ModelRow.model_name) == model_name,
            )
        )

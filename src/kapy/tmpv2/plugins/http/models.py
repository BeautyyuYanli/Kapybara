"""One-to-one model catalog HTTP adapters, borrowing an application-owned service."""

from uuid import UUID

from fastapi import APIRouter, Response

from kapy.tmpv2.control.models import (
    CreateModel,
    CreateProvider,
    ModelRecord,
    ModelService,
    ProviderRecord,
    UpdateModel,
    UpdateProvider,
)
from kapy.tmpv2.pagination import Page

from .dependencies import OffsetPage
from .errors import ControlRoute


def create_model_router(models: ModelService) -> APIRouter:
    router = APIRouter(route_class=ControlRoute)

    @router.post("/providers", status_code=201, operation_id="create_provider")
    async def create_provider(data: CreateProvider) -> ProviderRecord:
        return await models.create_provider(data)

    @router.get("/providers", operation_id="list_providers")
    async def list_providers(page: OffsetPage) -> Page[ProviderRecord]:
        return await models.list_providers(**page.model_dump())

    @router.get("/providers/{provider_id}", operation_id="get_provider")
    async def get_provider(provider_id: UUID) -> ProviderRecord:
        return await models.get_provider(provider_id)

    @router.patch("/providers/{provider_id}", operation_id="update_provider")
    async def update_provider(provider_id: UUID, data: UpdateProvider) -> ProviderRecord:
        return await models.update_provider(provider_id, data)

    @router.delete("/providers/{provider_id}", status_code=204, operation_id="delete_provider")
    async def delete_provider(provider_id: UUID) -> Response:
        await models.delete_provider(provider_id)
        return Response(status_code=204)

    @router.post("/providers/{provider_id}/discover-models", operation_id="discover_models")
    async def discover_models(provider_id: UUID) -> tuple[ModelRecord, ...]:
        return await models.discover_models(provider_id)

    @router.post("/models", status_code=201, operation_id="create_model")
    async def create_model(data: CreateModel) -> ModelRecord:
        return await models.create_model(data)

    @router.get("/models", operation_id="list_models")
    async def list_models(page: OffsetPage, provider_id: UUID | None = None) -> Page[ModelRecord]:
        return await models.list_models(provider_id=provider_id, **page.model_dump())

    @router.get("/models/{provider_id}/{model_name:path}", operation_id="get_model")
    async def get_model(provider_id: UUID, model_name: str) -> ModelRecord:
        return await models.get_model(provider_id, model_name)

    @router.patch("/models/{provider_id}/{model_name:path}", operation_id="update_model")
    async def update_model(provider_id: UUID, model_name: str, data: UpdateModel) -> ModelRecord:
        return await models.update_model(provider_id, model_name, data)

    @router.delete(
        "/models/{provider_id}/{model_name:path}", status_code=204, operation_id="delete_model"
    )
    async def delete_model(provider_id: UUID, model_name: str) -> Response:
        await models.delete_model(provider_id, model_name)
        return Response(status_code=204)

    return router

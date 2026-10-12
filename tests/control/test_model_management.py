"""Catalog/session services against PostgreSQL and the SDK's real protocol adapters.

Only HTTP transport is substituted: SDK pagination, request conversion, normalized
usage and native Provider/Model contexts execute normally.
"""

import asyncio
import inspect
import os
from uuid import uuid4

import httpx2
import pytest
import pytest_asyncio
from pydantic import SecretStr, ValidationError
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kapy.control.models import (
    CreateModel,
    CreateProvider,
    ModelAlreadyExists,
    ModelDiscoveryError,
    ModelService,
    UpdateModel,
    UpdateProvider,
)
from kapy.control.models.repository import ModelRepository
from kapy.control.sessions import CreateSession, SessionService, UpdateSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def sdk_http(monkeypatch):
    clients = []

    def install(handler):
        def create():
            client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
            clients.append(client)
            return client

        for method in (OpenAIProvider._get_http_client, GoogleProvider._build_http_options):
            module = inspect.getmodule(method)
            monkeypatch.setattr(module, "create_async_httpx2_client", create)
        return clients

    return install


@pytest_asyncio.fixture
async def single_connection_factory(database):
    """Exercise network boundaries with no spare connection to hide a held transaction."""
    engine = create_async_engine(
        database.engine.url,
        connect_args={"options": f"-csearch_path={database.schema},pg_catalog"},
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def create_provider(catalog, *, google=False, responses=False, **overrides):
    data = dict(
        name="provider",
        provider_class="pydantic_ai.providers.google:GoogleProvider"
        if google
        else "pydantic_ai.providers.openai:OpenAIProvider",
        model_class="pydantic_ai.models.google:GoogleModel"
        if google
        else (
            "pydantic_ai.models.openai:OpenAIResponsesModel"
            if responses
            else "pydantic_ai.models.openai:OpenAIChatModel"
        ),
        api_key="stored-secret",
        base_url="https://models.example" if google else "https://models.example/v1",
    )
    return await catalog.create_provider(CreateProvider.model_validate(data | overrides))


async def test_catalog_crud_references_and_session_settings(database, sdk_http):
    def unexpected(request):
        raise AssertionError("CRUD must not request a remote model")

    clients = sdk_http(unexpected)
    catalog, sessions = ModelService(database.sessions), SessionService(database.sessions)
    provider = await create_provider(catalog)
    assert "stored-secret" not in provider.model_dump_json()
    first = await catalog.create_model(
        CreateModel(
            provider_id=provider.id,
            model_name="custom",
            settings={"temperature": 0.1},
            context_window=1000,
        )
    )
    assert not clients  # Explicit capacity avoids constructing an SDK client.
    with pytest.raises(ModelAlreadyExists):
        await catalog.create_model(
            CreateModel(provider_id=provider.id, model_name="custom", context_window=1000)
        )
    unknown = await catalog.create_model(
        CreateModel(provider_id=provider.id, model_name="unknown-local-model")
    )
    assert unknown.context_window is None and clients[-1].is_closed
    updated = await catalog.update_model(
        provider.id,
        first.model_name,
        UpdateModel(name="display", settings={"temperature": 0.2}, context_window=None),
    )
    assert updated.name == "display" and updated.context_window is None
    assert updated.created_at == first.created_at and updated.updated_at >= first.updated_at
    session = await sessions.create_session(
        CreateSession(
            provider_id=provider.id,
            model_name=first.model_name,
            model_settings={"max_tokens": 20},
        )
    )
    assert session.model_settings == {"max_tokens": 20}
    updated_session = await sessions.update_session(
        session.id,
        UpdateSession(model_settings={}),
    )
    assert updated_session.model_settings == {}
    assert (await sessions.list_sessions(provider_id=provider.id)).items == [updated_session]
    assert (await sessions.list_sessions(model_name=first.model_name)).items == [updated_session]
    assert (
        await sessions.list_sessions(provider_id=uuid4(), model_name=first.model_name)
    ).items == []
    assert (await catalog.list_models(provider_id=provider.id, offset=1)).items == [unknown]
    await catalog.delete_model(provider.id, first.model_name)
    await catalog.delete_provider(provider.id)
    assert (await sessions.get_session(session.id)).provider_id == provider.id
    other = await create_provider(catalog, google=True)
    google = await catalog.create_model(
        CreateModel(provider_id=other.id, model_name="models/custom", context_window=1000)
    )
    assert google.model_name == "custom"
    await sessions.update_session(
        session.id, UpdateSession(provider_id=other.id, model_name=google.model_name)
    )
    await catalog.delete_provider(provider.id)
    await catalog.delete_provider(provider.id)
    assert (await catalog.list_models(provider_id=provider.id)).items == []
    assert (await catalog.list_providers()).items == [other]
    with pytest.raises(LookupError):
        await catalog.get_provider(provider.id)


async def test_sdk_settings_validation_and_update_semantics(database):
    catalog = ModelService(database.sessions)
    provider = await create_provider(catalog)
    model = await catalog.create_model(
        CreateModel(
            provider_id=provider.id,
            model_name="custom",
            context_window=1000,
            settings={"timeout": 10, "temperature": 0.4},
        )
    )
    sessions = SessionService(database.sessions)
    session = await sessions.create_session(
        CreateSession(
            provider_id=provider.id, model_name=model.model_name, model_settings={"max_tokens": 100}
        )
    )
    for invalid in ({"not_a_setting": 1}, {"temperature": {}}, {"timeout": {"read": 10}}):
        with pytest.raises(ValueError):
            await catalog.update_model(provider.id, model.model_name, UpdateModel(settings=invalid))
    with pytest.raises(ValidationError):
        UpdateSession(provider_id=uuid4())
    for data in (
        {"model_settings": None},
        {"plugins": []},
        {"context_plugin": {}},
        {"compaction_threshold_tokens": 50},
        {"title": None},
    ):
        with pytest.raises(ValidationError):
            UpdateSession.model_validate(data)
    await sessions.update_session(session.id, UpdateSession(model_settings={"google_top_k": 1}))
    saved = await sessions.get_session(session.id)
    assert saved.model_settings == {"google_top_k": 1}
    from temporalio.client import Client

    temporal = await Client.connect(os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"))
    executing = SessionService(database.sessions, temporal_client=temporal, temporal_task_queue="q")
    with pytest.raises(ValueError):
        await executing.start_runner(session.id, user_prompt="go")
    assert not await executing.is_runner_running(session.id)
    changed = await catalog.update_provider(
        provider.id, UpdateProvider(api_key=SecretStr("new-secret"), base_url=None)
    )
    assert changed.base_url is None
    async with database.sessions.begin() as db:
        config = await ModelRepository(db).get_provider_config(provider.id)
    assert config.api_key.get_secret_value() == "new-secret"
    assert (await catalog.get_model(provider.id, model.model_name)).settings == model.settings
    for offset, limit in [(-1, 1), (0, 0), (0, 201)]:
        with pytest.raises(ValueError):
            await sessions.list_sessions(offset=offset, limit=limit)


async def test_provider_rejects_invalid_imports_constructor_arguments_and_urls(database):
    catalog = ModelService(database.sessions)
    for overrides in (
        {"provider_class": "pydantic_ai.providers.openai.OpenAIProvider"},
        {"model_class": "builtins:str"},
        {"model_class": "pydantic_ai.models:Model"},
        {"provider_kwargs": {"http_client": {}}},
        {"provider_kwargs": {"unknown": 1}},
        {"base_url": "https://user:password@example.com"},
        {"base_url": "https://bad host/v1"},
        {"base_url": "https://example.com?secret=value"},
        {"api_key": ""},
    ):
        with pytest.raises(ValueError):
            await create_provider(catalog, **overrides)


async def test_discovery_uses_sdk_profiles_preserves_edits_and_rolls_back_failed_page(
    single_connection_factory, sdk_http
):
    pages = []
    fail = False

    async def remote(request):
        async with asyncio.timeout(1), single_connection_factory.begin() as db:
            assert (await db.execute(text("SELECT 1"))).scalar_one() == 1
        pages.append(str(request.url))
        assert request.headers["x-goog-api-key"] == "stored-secret"
        if request.url.params.get("pageToken") == "next":
            if fail:
                return httpx2.Response(
                    400,
                    json={
                        "error": {
                            "code": 400,
                            "message": "page failed",
                            "status": "INVALID_ARGUMENT",
                        }
                    },
                )
            return httpx2.Response(
                200,
                json={
                    "models": [
                        {
                            "name": "models/gemini-2.5-flash",
                            "supportedGenerationMethods": ["generateContent"],
                        }
                    ]
                },
            )
        return httpx2.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/custom",
                        "displayName": "Custom",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {"name": "models/embedding", "supportedGenerationMethods": ["embedContent"]},
                ],
                "nextPageToken": "next",
            },
        )

    clients = sdk_http(remote)
    catalog = ModelService(single_connection_factory)
    provider = await create_provider(
        catalog, google=True, provider_kwargs={"retry_options": {"attempts": 1}}
    )
    first = await catalog.discover_models(provider.id)
    assert [model.model_name for model in first] == ["custom", "gemini-2.5-flash"]
    assert [model.name for model in first] == ["Custom", "gemini-2.5-flash"]
    assert first[0].context_window is None
    assert first[1].context_window is not None and first[1].context_window > 0
    assert len(pages) == 2 and all(client.is_closed for client in clients)
    manual = await catalog.update_model(
        provider.id,
        "gemini-2.5-flash",
        UpdateModel(name="edited", settings={"temperature": 0.1}, context_window=None),
    )
    assert (await catalog.discover_models(provider.id))[1] == manual
    await catalog.delete_model(provider.id, "custom")
    fail = True
    with pytest.raises(ModelDiscoveryError):
        await catalog.discover_models(provider.id)
    assert (await catalog.list_models(provider_id=provider.id)).items == [manual]
    fail = False
    assert len(await catalog.discover_models(provider.id)) == 2
    assert all(client.is_closed for client in clients)


async def test_create_infers_sdk_capacity_and_openai_discovery_keeps_existing(database, sdk_http):
    def remote(request):
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == "Bearer stored-secret"
        return httpx2.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {"id": "gpt-4.1", "object": "model", "created": 0, "owned_by": "test"},
                    {"id": "custom", "object": "model", "created": 0, "owned_by": "test"},
                ],
            },
        )

    clients = sdk_http(remote)
    catalog = ModelService(database.sessions)
    provider = await create_provider(catalog, responses=True)
    inferred = await catalog.create_model(
        CreateModel(provider_id=provider.id, model_name="gpt-4.1")
    )
    assert inferred.context_window is not None and inferred.context_window > 0
    models = await catalog.discover_models(provider.id)
    assert [model.model_name for model in models] == ["custom", "gpt-4.1"]
    assert models[1] == inferred and all(client.is_closed for client in clients)

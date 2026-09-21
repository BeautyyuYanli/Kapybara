"""Catalog/session services against PostgreSQL and the SDK's real protocol adapters.

Only HTTP transport is substituted: SDK pagination, request conversion, normalized
usage and native Provider/Model contexts execute normally.
"""

import asyncio
import inspect
import json
from uuid import uuid4

import httpx2
import pytest
import pytest_asyncio
from pydantic import SecretStr, ValidationError
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.google import GoogleProvider
from pydantic_ai.providers.openai import OpenAIProvider
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kapy.agent_runner import SessionBusy, open_runner
from kapy.agent_runner.context_summary import COMPACTION_PROMPT
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
from kapy.session_lease import is_session_busy
from kapy.session_lease.models import SessionLeaseRow

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
            compaction_threshold_tokens=50,
        )
    )
    assert session.model_settings == {"max_tokens": 20}
    updated_session = await sessions.update_session(
        session.id,
        UpdateSession(
            model_settings={}, compaction_threshold_tokens=None, compaction_replay_turns=0
        ),
    )
    assert (
        updated_session.model_settings == {} and updated_session.compaction_threshold_tokens is None
    )
    assert updated_session.compaction_replay_turns == 0
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
        {"compaction_replay_turns": None},
        {"compaction_threshold_tokens": True},
        {"title": None},
    ):
        with pytest.raises(ValidationError):
            UpdateSession.model_validate(data)
    await sessions.update_session(session.id, UpdateSession(model_settings={"google_top_k": 1}))
    saved = await sessions.get_session(session.id)
    assert saved.model_settings == {"google_top_k": 1}
    queued = await sessions.enqueue_input(session.id, "queued", "go")
    with pytest.raises(ValueError):
        await sessions.start_runner(session.id, agent=Agent())
    assert await sessions.read_inputs(session.id, "queued") == (queued,)
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


@pytest.mark.parametrize("context_tokens,stored_capacity", [(700, 1000), (701, 1000), (700, None)])
async def test_session_configuration_applies_through_queued_runs_and_compaction(
    single_connection_factory, sdk_http, monkeypatch, context_tokens, stored_capacity
):
    calls = []
    session_id = None
    sessions = SessionService(single_connection_factory)
    catalog = ModelService(single_connection_factory)
    in_scope = []
    completed = []
    original_enter = OpenAIChatModel.__aenter__
    original_exit = OpenAIChatModel.__aexit__

    async def enter(model):
        result = await original_enter(model)
        in_scope.append(model)
        return result

    async def exit(model, *args):
        try:
            return await original_exit(model, *args)
        finally:
            in_scope.remove(model)
            completed.append(model)

    monkeypatch.setattr(OpenAIChatModel, "__aenter__", enter)
    monkeypatch.setattr(OpenAIChatModel, "__aexit__", exit)

    async def remote(request):
        payload = json.loads(request.content)
        summary = any(message["content"] == COMPACTION_PROMPT for message in payload["messages"])
        calls.append(summary)
        assert in_scope
        assert in_scope[0].profile["context_window"] == stored_capacity
        assert payload["model"] == "gpt-4.1" and payload["temperature"] == 0.4
        assert payload["max_completion_tokens"] == 25
        assert any(message["content"] == "caller instructions" for message in payload["messages"])
        assert session_id is not None
        # A second transaction must acquire the only connection while the SDK is waiting.
        async with asyncio.timeout(1):
            assert await sessions.is_runner_running(session_id)
            if len(calls) == 1:
                await sessions.update_session(
                    session_id, UpdateSession(model_settings={"temperature": 0.9})
                )
        return httpx2.Response(
            200,
            json={
                "id": f"response-{len(calls)}",
                "object": "chat.completion",
                "created": 0,
                "model": "gpt-4.1",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": context_tokens,
                    "completion_tokens": 0,
                    "total_tokens": context_tokens,
                },
            },
        )

    clients = sdk_http(remote)
    provider = await create_provider(catalog)
    await catalog.create_model(
        CreateModel(
            provider_id=provider.id,
            model_name="gpt-4.1",
            context_window=1000,
            settings={"temperature": 0.1, "max_tokens": 25},
        )
    )
    if stored_capacity is None:
        await catalog.update_model(provider.id, "gpt-4.1", UpdateModel(context_window=None))
    session = await sessions.create_session(
        CreateSession(
            provider_id=provider.id,
            model_name="gpt-4.1",
            model_settings={"temperature": 0.4},
            compaction_threshold_tokens=700 if stored_capacity is None else None,
        )
    )
    session_id = session.id
    agent = Agent(instructions="caller instructions", model_settings={"temperature": 0.8})
    await sessions.enqueue_input(session.id, "steer", "first")
    await sessions.enqueue_input(session.id, "queued", "second")
    result = await sessions.start_runner(session.id, agent=agent)
    assert result.finished and result.output == "answer"
    assert calls == ([False, True, False, True] if context_tokens > 700 else [False, False])
    assert completed and not in_scope
    assert all(client.is_closed for client in clients)
    assert agent.model is None and agent.model_settings == {"temperature": 0.8}
    assert not await sessions.is_runner_running(session.id)
    assert (await sessions.get_session(session.id)).model_settings == {"temperature": 0.9}


@pytest.mark.parametrize(
    "context_window,threshold_fields,expected",
    [
        (None, {}, 183500),
        (None, {"compaction_threshold_tokens": None}, 183500),
        (None, {"compaction_threshold_tokens": 50}, 50),
        (1000, {}, None),
        (1000, {"compaction_threshold_tokens": None}, None),
        (1000, {"compaction_threshold_tokens": 50}, 50),
    ],
)
async def test_session_creation_persists_threshold_default(
    database, sdk_http, context_window, threshold_fields, expected
):
    sdk_http(lambda request: pytest.fail("No model request expected"))
    catalog, sessions = ModelService(database.sessions), SessionService(database.sessions)
    provider = await create_provider(catalog)
    model = await catalog.create_model(
        CreateModel(provider_id=provider.id, model_name="custom", context_window=context_window)
    )
    session = await sessions.create_session(
        CreateSession.model_validate(
            {"provider_id": provider.id, "model_name": model.model_name} | threshold_fields
        )
    )
    assert session.compaction_threshold_tokens == expected
    assert (await sessions.get_session(session.id)).compaction_threshold_tokens == expected


async def test_threshold_required_before_any_input_is_consumed(database, sdk_http):
    clients = sdk_http(lambda request: pytest.fail("No model request expected"))
    catalog, sessions = ModelService(database.sessions), SessionService(database.sessions)
    provider = await create_provider(catalog)
    await catalog.create_model(CreateModel(provider_id=provider.id, model_name="custom"))
    clients.clear()
    session = await sessions.create_session(
        CreateSession(provider_id=provider.id, model_name="custom")
    )
    await sessions.update_session(session.id, UpdateSession(compaction_threshold_tokens=None))
    assert (await sessions.get_session(session.id)).compaction_threshold_tokens is None
    queued = await sessions.enqueue_input(session.id, "queued", "go")
    with pytest.raises(ValueError, match="compaction_threshold_tokens"):
        await sessions.start_runner(session.id, agent=Agent())
    assert await sessions.read_inputs(session.id, "queued") == (queued,)
    assert not await sessions.is_runner_running(session.id)
    assert not clients


async def test_running_status_observes_fresh_done_and_expired_leases(database):
    catalog = ModelService(database.sessions)
    sessions = SessionService(database.sessions, heartbeat_interval=1, heartbeat_timeout=30)
    provider = await create_provider(catalog)
    await catalog.create_model(
        CreateModel(provider_id=provider.id, model_name="custom", context_window=1000)
    )
    session = await sessions.create_session(
        CreateSession(provider_id=provider.id, model_name="custom")
    )
    assert not await sessions.is_runner_running(session.id)
    async with open_runner(session.id, agent=Agent("test"), session_factory=database.sessions):
        assert await sessions.is_runner_running(session.id)  # done is still owned.
        async with database.sessions.begin() as db:
            await db.execute(
                text(
                    "UPDATE session_leases "
                    "SET heartbeat_at=clock_timestamp() - interval '45 seconds' "
                    "WHERE session_id=:id"
                ),
                {"id": session.id},
            )
        assert not await sessions.is_runner_running(session.id)
    assert not await sessions.is_runner_running(uuid4())
    assert await sessions.read_inputs(uuid4(), "queued") == ()
    with pytest.raises(LookupError):
        await sessions.enqueue_input(uuid4(), "queued", "orphan")
    for interval, timeout in [(0, 60), (60, 60), (10, float("inf")), (float("nan"), 60)]:
        with pytest.raises(ValueError):
            SessionService(
                database.sessions, heartbeat_interval=interval, heartbeat_timeout=timeout
            )


async def test_shared_agent_keeps_session_overrides_isolated_and_busy_closes_client(
    database, sdk_http
):
    entered = asyncio.Queue()
    finish = asyncio.Event()
    requests = []

    async def remote(request):
        body = json.loads(request.content)
        requests.append((body["model"], body["temperature"]))
        await entered.put(body["model"])
        await finish.wait()
        return httpx2.Response(
            200,
            json={
                "id": "response",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": body["model"]},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    clients = sdk_http(remote)
    catalog, sessions = ModelService(database.sessions), SessionService(database.sessions)
    provider = await create_provider(catalog)
    ids = []
    for name, temperature in (("first", 0.2), ("second", 0.7)):
        await catalog.create_model(
            CreateModel(provider_id=provider.id, model_name=name, context_window=1000)
        )
        record = await sessions.create_session(
            CreateSession(
                provider_id=provider.id,
                model_name=name,
                model_settings={"temperature": temperature},
            )
        )
        ids.append(record.id)
        await sessions.enqueue_input(record.id, "queued", "go")
    agent = Agent()
    tasks = [asyncio.create_task(sessions.start_runner(id, agent=agent)) for id in ids]
    try:
        async with asyncio.timeout(3):
            assert {await entered.get(), await entered.get()} == {"first", "second"}
        with pytest.raises(SessionBusy):
            await sessions.start_runner(ids[0], agent=agent)
        assert clients[-1].is_closed and not clients[0].is_closed
        finish.set()
        results = await asyncio.gather(*tasks)
        assert [result.output for result in results] == ["first", "second"]
        assert sorted(requests) == [("first", 0.2), ("second", 0.7)]
        assert all(client.is_closed for client in clients)
    finally:
        finish.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def test_service_uses_configured_timeout_and_renews_while_model_waits(
    database, sdk_http, heartbeat_observation
):
    entered, finish = asyncio.Event(), asyncio.Event()
    _, heartbeat_called = heartbeat_observation

    async def remote(request):
        entered.set()
        await finish.wait()
        return httpx2.Response(
            200,
            json={
                "id": "response",
                "object": "chat.completion",
                "created": 0,
                "model": "custom",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "answer"},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    sdk_http(remote)
    catalog = ModelService(database.sessions)
    sessions = SessionService(database.sessions, heartbeat_interval=0.01, heartbeat_timeout=30)
    provider = await create_provider(catalog)
    await catalog.create_model(
        CreateModel(provider_id=provider.id, model_name="custom", context_window=1000)
    )
    session = await sessions.create_session(
        CreateSession(provider_id=provider.id, model_name="custom")
    )
    async with database.sessions.begin() as db:
        db.add(SessionLeaseRow(session_id=session.id, lock_token=uuid4()))
        await db.flush()
        await db.execute(
            text(
                "UPDATE session_leases "
                "SET heartbeat_at=clock_timestamp() - interval '45 seconds' "
                "WHERE session_id=:id"
            ),
            {"id": session.id},
        )
        assert await is_session_busy(db, session.id, heartbeat_timeout=60)
    assert not await sessions.is_runner_running(session.id)
    await sessions.enqueue_input(session.id, "queued", "go")
    task = asyncio.create_task(sessions.start_runner(session.id, agent=Agent()))
    try:
        async with asyncio.timeout(3):
            await entered.wait()
            heartbeat_called.clear()
            await heartbeat_called.wait()
        assert not task.done()
        assert await sessions.is_runner_running(session.id)
        finish.set()
        result = await task
        assert result.finished and result.output == "answer"
    finally:
        finish.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

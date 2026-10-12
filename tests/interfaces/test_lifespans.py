"""Interface wiring against PostgreSQL, Valkey and Temporal; Telegram sends are captured."""

import asyncio
import os
import time
from itertools import pairwise
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from sqlalchemy.ext.asyncio import async_sessionmaker

from kapy.agent_history import HistoryMessage
from kapy.agent_output import AgentOutputService, MessageCommitted, TextDelta
from kapy.application.settings import CommonSettings
from kapy.control.models import CreateModel, CreateProvider, ModelService
from kapy.control.sessions import CreateSession, SessionService
from kapy.interfaces.http.app import create_app
from kapy.interfaces.http.settings import HttpSettings
from kapy.interfaces.telegram.client import TelegramClient
from kapy.interfaces.telegram.controller import TelegramController
from kapy.interfaces.telegram.delivery import TelegramDelivery
from kapy.interfaces.telegram.models import DeliveryRow
from kapy.interfaces.telegram.repository import TelegramRepository, delivery_key
from kapy.interfaces.telegram.schema import migrate as migrate_telegram
from kapy.interfaces.telegram.settings import TelegramSettings
from kapy.interfaces.telegram.storage import open_storage

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_telegram_model_command_updates_only_bound_session_and_new_defaults(
    database, tmp_path
):
    models = ModelService(database.sessions)
    providers = []
    for name in ("old", "new"):
        provider = await models.create_provider(
            CreateProvider(
                name=name,
                provider_class="pydantic_ai.providers.openai:OpenAIProvider",
                model_class="pydantic_ai.models.openai:OpenAIResponsesModel",
                api_key=SecretStr("local-test"),
            )
        )
        await models.create_model(
            CreateModel(provider_id=provider.id, model_name=name, context_window=10000)
        )
        providers.append(provider)
    old_provider, new_provider = providers
    sessions = SessionService(database.sessions)
    path = tmp_path / "telegram.sqlite3"
    await migrate_telegram(path, "upgrade")
    async with open_storage(path) as engine:
        repository = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        controller = TelegramController(
            client=AsyncMock(spec=TelegramClient),
            sessions=sessions,
            models=models,
            repository=repository,
            settings=TelegramSettings(
                bot_token="unused",
                allowed_chat_ids={123},
                database_path=path,
                session_template=CreateSession(
                    provider_id=old_provider.id,
                    model_name="old",
                    title="keep title",
                    model_settings={"temperature": 0.3},
                ),
            ),
            bot_id=42,
            username="kapy_bot",
        )

        async def send(update_id, text, thread=0):
            await repository.ingest(
                42,
                [
                    {
                        "update_id": update_id,
                        "message": {
                            "chat": {"id": 123, "type": "private"},
                            "from": {"id": 1, "is_bot": False},
                            "message_thread_id": thread,
                            "text": text,
                        },
                    }
                ],
            )
            assert await controller.process_once()

        await send(1, "/new")
        await send(2, "/new", thread=9)
        target = await repository.route(42, 123, 0)
        other = await repository.route(42, 123, 9)
        assert target is not None and other is not None
        await send(3, f"/model {new_provider.id} new")
        changed = await sessions.get_session(target)
        assert (changed.provider_id, changed.model_name) == (new_provider.id, "new")
        assert changed.title == "keep title" and changed.model_settings == {"temperature": 0.3}
        assert (await sessions.get_session(other)).provider_id == old_provider.id
        assert await repository.route(42, 123, 0) == target
        await send(4, "/new", thread=18)
        created_id = await repository.route(42, 123, 18)
        assert created_id is not None
        created = await sessions.get_session(created_id)
        assert (created.provider_id, created.model_name) == (new_provider.id, "new")
        assert created.title == "keep title" and created.model_settings == {"temperature": 0.3}
        assert len((await models.list_models()).items) == 2


@pytest.mark.asyncio
async def test_http_interface_serves_http_without_credentials(database, monkeypatch):
    monkeypatch.delenv("KAPY_CONTROL_TOKEN", raising=False)
    settings = HttpSettings(
        common=CommonSettings(
            temporal_address=os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
            database_url=SecretStr(database.url),
            database_schema=database.schema,
        ),
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        from temporalio.api.workflowservice.v1 import GetSystemInfoRequest

        temporal_client = app.state.resources.temporal_client
        await temporal_client.workflow_service.get_system_info(GetSystemInfoRequest())
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            for path in ("/api/providers", "/api/models", "/api/sessions"):
                result = await client.get(path)
                assert result.status_code == 200
                assert result.json() == {"items": [], "has_more": False}


@pytest.mark.asyncio
async def test_http_shutdown_joins_start_request_before_resources_close(
    database, seed_session, monkeypatch
):
    from contextlib import asynccontextmanager

    from kapy.interfaces.http import app as http_app

    session_id = uuid4()
    await seed_session(session_id)
    events = []
    started = asyncio.Event()
    original_resources = http_app.open_resources

    @asynccontextmanager
    async def resources(settings):
        async with original_resources(settings) as value:
            yield value
        events.append("resources-closed")

    async def runner(self, target, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            await self.get_session(target)
            events.append("runner-cleaned")

    monkeypatch.setattr(http_app, "open_resources", resources)
    monkeypatch.setattr(SessionService, "start_durable_runner", runner)
    app = create_app(
        HttpSettings(
            common=CommonSettings(
                temporal_address=os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
                database_url=SecretStr(database.url),
                database_schema=database.schema,
            ),
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        async with app.router.lifespan_context(app):
            request = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session_id}/runner",
                    json={"user_prompt": "test"},
                )
            )
            await asyncio.wait_for(started.wait(), 2)
        (result,) = await asyncio.gather(request, return_exceptions=True)
        assert isinstance(result, asyncio.CancelledError)
    assert events == ["runner-cleaned", "resources-closed"]


@pytest.mark.asyncio
async def test_telegram_shutdown_joins_workers_before_resources_close(
    database, tmp_path, monkeypatch
):
    from contextlib import asynccontextmanager

    from kapy.interfaces.telegram import main as telegram_main

    models = ModelService(database.sessions)
    provider = await models.create_provider(
        CreateProvider(
            name="shutdown-check",
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="pydantic_ai.models.openai:OpenAIResponsesModel",
            api_key=SecretStr("test"),
        )
    )
    model = await models.create_model(
        CreateModel(provider_id=provider.id, model_name="test", context_window=10000)
    )
    path = tmp_path / "telegram.sqlite3"
    await migrate_telegram(path, "upgrade")
    settings = TelegramSettings(
        common=CommonSettings(
            temporal_address=os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
            database_url=SecretStr(database.url),
            database_schema=database.schema,
        ),
        database_path=path,
        bot_token="unused",
        allowed_chat_ids={123},
        session_template=CreateSession(provider_id=provider.id, model_name=model.model_name),
    )
    started = asyncio.Event()
    events = []
    original_resources = telegram_main.open_resources

    @asynccontextmanager
    async def resources(config):
        try:
            async with original_resources(config) as value:
                yield value
        finally:
            events.append("resources-closed")

    async def api(self, method, params):
        if method == "getMe":
            return {"id": 42, "username": "kapy_bot"}
        if method == "setMyCommands":
            assert {item["command"] for item in params["commands"]} == {
                "new",
                "status",
                "close",
                "model",
                "help",
            }
            return True
        if method == "getUpdates" and params["offset"] == 0:
            return [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 123, "type": "private"},
                        "from": {"id": 1, "is_bot": False},
                        "text": "test input",
                    },
                }
            ]
        await asyncio.Future()

    async def runner(self, target, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            await self.get_session(target)
            events.append("runner-cleaned")

    monkeypatch.setattr(telegram_main, "open_resources", resources)
    monkeypatch.setattr(TelegramClient, "api", api)
    monkeypatch.setattr(SessionService, "start_durable_runner", runner)
    task = asyncio.create_task(telegram_main.serve(settings))
    try:
        await asyncio.wait_for(started.wait(), 3)
    finally:
        task.cancel()
        (result,) = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError)
    assert events == ["runner-cleaned", "resources-closed"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("configured_interval", "interval"), [(0.2, 0.2), (0, 0.5)])
async def test_telegram_serve_draft_pacing_uses_publisher_setting(
    database, seed_session, tmp_path, monkeypatch, configured_interval, interval
):
    from kapy.interfaces.telegram import main as telegram_main

    session_id = uuid4()
    await seed_session(session_id)
    path = tmp_path / "telegram.sqlite3"
    await migrate_telegram(path, "upgrade")
    monkeypatch.setenv("KAPY_DATABASE_URL", database.url)
    monkeypatch.setenv("KAPY_DATABASE_SCHEMA", database.schema)
    monkeypatch.setenv("KAPY_OUTPUT_FLUSH_INTERVAL", str(configured_interval))
    settings = TelegramSettings(
        database_path=path,
        bot_token="unused",
        allowed_chat_ids={123},
        session_template=None,
    )
    row = DeliveryRow(
        bot_id=42, chat_id=123, thread_id=0, session_id=session_id, chat_type="private"
    )
    sent = [asyncio.Event() for _ in range(3)]
    settled = asyncio.Event()
    drafts, messages, times = [], [], []

    async def api(self, method, params):
        if method == "getMe":
            return {"id": 42, "username": "kapy_bot"}
        if method == "setMyCommands":
            return True
        if method == "getUpdates":
            await asyncio.Future()
        content = params["rich_message"]["markdown"]
        if method == "sendRichMessageDraft":
            drafts.append(content)
            times.append(time.monotonic())
            # Network work consumes part of the period rather than extending it.
            await asyncio.sleep(0.15)
            sent[int(content)].set()
            return True
        assert method == "sendRichMessage"
        messages.append(content)
        return {"message_id": 123}

    async def live(self, target, *, after_seq):
        times.append(time.monotonic())
        for index in range(3):
            yield [TextDelta(target, 0, 0, "text", "replace", str(index))]
            await sent[index].wait()
        yield [MessageCommitted(HistoryMessage(target, 0, ModelResponse([TextPart("final")])))]
        settled.set()
        await asyncio.Future()

    monkeypatch.setattr(TelegramClient, "api", api)
    monkeypatch.setattr(SessionService, "live", live)
    async with open_storage(path) as engine:
        repository = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        await repository.save_delivery(row)
        task = asyncio.create_task(telegram_main.serve(settings))
        try:
            await asyncio.wait_for(settled.wait(), 4)
            assert drafts == ["0", "1", "2"]
            assert all(
                interval - 0.02 <= end - start < interval + 0.12
                for start, end in pairwise(times[1:])
            )
            assert messages == ["final"]
            assert (await repository.get_delivery(delivery_key(row))).after_seq == 0
        finally:
            task.cancel()
            (result,) = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result, asyncio.CancelledError)


@pytest.mark.asyncio
async def test_http_interface_mounts_existing_frontend_with_api(database, tmp_path):
    (tmp_path / "index.html").write_text("<main>existing frontend</main>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app.js").write_text("export {}")
    app = create_app(
        HttpSettings(
            common=CommonSettings(
                temporal_address=os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
                database_url=SecretStr(database.url),
                database_schema=database.schema,
            ),
            frontend_dist=tmp_path,
        )
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            for path in ("/app/", "/app/providers/123", "/app/sessions/123"):
                result = await client.get(path, headers={"accept": "text/html"})
                assert result.status_code == 200 and "existing frontend" in result.text
            assert (await client.get("/app/assets/app.js")).status_code == 200
            assert (await client.get("/app/assets/missing.js")).status_code == 404
            assert (await client.get("/api/missing")).status_code == 404
            result = await client.get("/api/sessions")
            assert result.status_code == 200
            assert (await client.get("/openapi.json")).json()["paths"]["/api/sessions"]


@pytest.mark.asyncio
async def test_http_interface_rejects_missing_frontend_entry(tmp_path):
    app = create_app(HttpSettings(frontend_dist=tmp_path))
    with pytest.raises(RuntimeError, match="Frontend entry point"):
        async with app.router.lifespan_context(app):
            pytest.fail("A missing frontend entry must fail application setup")


@pytest.mark.asyncio
async def test_telegram_reconnects_after_real_subscription_setup_timeout(
    database,
    valkey_client,
    tmp_path,
    seed_history,
    monkeypatch,
):
    from pydantic_ai.messages import ModelResponse, TextPart

    from kapy.interfaces.telegram import delivery as delivery_module
    from kapy.interfaces.telegram.models import DeliveryRow

    session_id = await seed_history(
        [
            ModelRequest([UserPromptPart("already processed")]),
            ModelResponse([TextPart("recovered")]),
        ]
    )
    original_pubsub = valkey_client.pubsub
    attempts = 0
    timed_out_closed = asyncio.Event()

    class SlowConnect:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            timed_out_closed.set()

        async def connect(self):
            await asyncio.Future()

    def pubsub():
        nonlocal attempts
        attempts += 1
        return SlowConnect() if attempts == 1 else original_pubsub()

    monkeypatch.setattr(valkey_client, "pubsub", pubsub)
    monkeypatch.setattr(delivery_module, "retry_delay", lambda *args: 0)
    sessions = SessionService(
        database.sessions,
        output_service=AgentOutputService(
            valkey_client,
            channel_prefix="setup-timeout:" + uuid4().hex,
        ),
    )
    cursors = []
    original_live = sessions.live

    def live(target, *, after_seq):
        cursors.append(after_seq)
        return original_live(target, after_seq=after_seq)

    monkeypatch.setattr(sessions, "live", live)
    path = tmp_path / "telegram.sqlite3"
    await migrate_telegram(path, "upgrade")
    async with open_storage(path) as engine:
        repository = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        row = DeliveryRow(
            bot_id=42,
            chat_id=123,
            thread_id=0,
            session_id=session_id,
            chat_type="private",
            after_seq=0,
        )
        await repository.save_delivery(row)
        client = AsyncMock(spec=TelegramClient)
        task = asyncio.create_task(
            TelegramDelivery(client, sessions, repository, 42).follow(delivery_key(row))
        )
        try:
            async with asyncio.timeout(4):
                while (await repository.get_delivery(delivery_key(row))).after_seq != 1:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            assert attempts == 2 and cursors == [0, 0] and timed_out_closed.is_set()
            client.send.assert_awaited_once_with(123, 0, "recovered", rich=True)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

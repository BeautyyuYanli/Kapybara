"""Plugin wiring against real PostgreSQL/Valkey; Telegram network sends are captured."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import psycopg
import pytest
from alembic import command
from dotenv import dotenv_values
from psycopg import sql
from pydantic import SecretStr
from pydantic_ai.models.test import TestModel
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from kapy.tmpv2.agent_output import AgentOutputService
from kapy.tmpv2.agent_runner.models import agent_metadata
from kapy.tmpv2.application.agent import create_agent
from kapy.tmpv2.application.resources import open_core_database
from kapy.tmpv2.application.settings import CommonSettings
from kapy.tmpv2.control.database import ControlTable
from kapy.tmpv2.control.models import CreateModel, CreateProvider, ModelService
from kapy.tmpv2.control.sessions import CreateSession, SessionService
from kapy.tmpv2.database.migration import migration_config
from kapy.tmpv2.database.schema import OWNED_TABLES, migrate
from kapy.tmpv2.plugins.http.app import create_app
from kapy.tmpv2.plugins.http.settings import HttpSettings
from kapy.tmpv2.plugins.telegram.client import TelegramClient
from kapy.tmpv2.plugins.telegram.controller import TelegramController
from kapy.tmpv2.plugins.telegram.delivery import TelegramDelivery
from kapy.tmpv2.plugins.telegram.repository import TelegramRepository, delivery_key
from kapy.tmpv2.plugins.telegram.schema import migrate as migrate_telegram
from kapy.tmpv2.plugins.telegram.settings import TelegramSettings
from kapy.tmpv2.plugins.telegram.storage import open_storage

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_core_migrations_own_only_core_tables(database):
    schema = "tmpv2_migrations_" + uuid4().hex
    settings = CommonSettings(database_url=SecretStr(database.url), database_schema=schema)
    try:
        await migrate(settings, "upgrade")
        await migrate(settings, "upgrade")
        async with open_core_database(settings) as engine:
            async with engine.begin() as db:
                await db.execute(text("CREATE TABLE unrelated (id INTEGER)"))
                await db.execute(text("CREATE TABLE plugin_other_state (id INTEGER)"))
                tables = await db.run_sync(lambda conn: inspect(conn).get_table_names())
                assert set(tables) == OWNED_TABLES | {
                    "core_schema_version",
                    "unrelated",
                    "plugin_other_state",
                }

                def check(connection):
                    connection.dialect.default_schema_name = schema
                    config = migration_config(
                        connection,
                        directory=Path(__file__).parents[3] / "src/kapy/tmpv2/database/migrations",
                        metadata=[ControlTable.metadata, agent_metadata],
                        version_table="core_schema_version",
                        owns_table=OWNED_TABLES.__contains__,
                    )
                    command.check(config)

                await db.run_sync(check)
    finally:
        async with await psycopg.AsyncConnection.connect(database.url, autocommit=True) as db:
            await db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


async def session_round_trip(database, valkey_client, tmp_path, *, live_config=None):
    """Only Bot API sends are mocked; input, leases, runner, history and live are real."""
    settings_values = live_config or {}
    models = ModelService(database.sessions)
    provider = await models.create_provider(
        CreateProvider(
            name="plugin-check",
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="pydantic_ai.models.openai:OpenAIResponsesModel",
            api_key=SecretStr(settings_values.get("OPENAI_API_KEY") or "local-test"),
            base_url=settings_values.get("OPENAI_BASE_URL") or None,
        )
    )
    model = await models.create_model(
        CreateModel(
            provider_id=provider.id,
            model_name=settings_values.get("OPENAI_MODEL") or "test",
            settings={"max_tokens": 128, "timeout": 90},
            context_window=10000,
        )
    )
    prefix = "plugin-test:" + uuid4().hex
    sessions = SessionService(
        database.sessions,
        output_service=AgentOutputService(
            valkey_client,
            channel_prefix=prefix,
        ),
    )
    path = tmp_path / "telegram.sqlite3"
    await migrate_telegram(path, "upgrade")
    async with open_storage(path) as engine:
        repository = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        config = TelegramSettings(
            bot_token="not-used",
            allowed_chat_ids={123},
            database_path=path,
            session_template=CreateSession(
                provider_id=provider.id,
                model_name=model.model_name,
                compaction_threshold_tokens=10000,
            ),
        )
        client = AsyncMock(spec=TelegramClient)
        scheduled = []
        controller = TelegramController(
            client=client,
            sessions=sessions,
            repository=repository,
            settings=config,
            bot_id=42,
            username="kapy_bot",
            schedule_runner=scheduled.append,
        )
        await repository.ingest(
            42,
            [
                {
                    "update_id": 1,
                    "message": {
                        "chat": {"id": 123, "type": "private"},
                        "from": {"id": 1, "is_bot": False},
                        "text": "Reply with exactly KAPY_PLUGIN_OK and nothing else.",
                    },
                }
            ],
        )
        await controller.process_once()
        assert len(scheduled) == 1
        session_id = scheduled[0]
        (row,) = await repository.deliveries(42)
        delivery = TelegramDelivery(client, sessions, repository, 42)
        follower = asyncio.create_task(delivery.consume(row))
        try:
            async with asyncio.timeout(5):
                while (await valkey_client.pubsub_numsub(f"{prefix}:{session_id}"))[0][1] != 1:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            result = await sessions.start_runner(
                session_id,
                agent=create_agent(),
                realtime_output=True,
                output_flush_interval=0,
            )
            assert result.finished and result.output is not None
            assert result.output.strip() == "KAPY_PLUGIN_OK"
            async with asyncio.timeout(5):
                while (await repository.get_delivery(delivery_key(row))).after_seq < 1:  # noqa: ASYNC110
                    await asyncio.sleep(0.01)
            assert not await sessions.read_inputs(session_id, "queued")
            assert not await sessions.is_runner_running(session_id)
            assert len((await sessions.read_history(session_id)).items) == 2
            assert [
                call.args[2]
                for call in client.send.call_args_list
                if call.kwargs.get("draft_id") is None
            ] == ["KAPY_PLUGIN_OK"]
        finally:
            follower.cancel()
            await asyncio.gather(follower, return_exceptions=True)
        assert (await valkey_client.pubsub_numsub(f"{prefix}:{session_id}"))[0][1] == 0


@pytest.mark.asyncio
async def test_telegram_core_runner_live_round_trip(
    database, valkey_client, tmp_path, session_model
):
    session_model(TestModel(custom_output_text="KAPY_PLUGIN_OK"))
    await session_round_trip(database, valkey_client, tmp_path)


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("KAPY_PLUGIN_LIVE_CHECK") != "1", reason="Explicit paid-model opt-in"
)
@pytest.mark.asyncio
async def test_real_model_telegram_round_trip(database, valkey_client, tmp_path):
    config = dotenv_values(".env")
    assert config.get("OPENAI_API_KEY") and config.get("OPENAI_MODEL"), (
        "Live model is not configured"
    )
    await session_round_trip(database, valkey_client, tmp_path, live_config=config)


@pytest.mark.asyncio
async def test_http_plugin_authorizes_before_business_work(database):
    settings = HttpSettings(
        common=CommonSettings(
            database_url=SecretStr(database.url), database_schema=database.schema
        ),
        control_token="operator-secret",
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as client:
            assert (await client.get("/api/sessions")).status_code == 401
            result = await client.get(
                "/api/sessions", headers={"Authorization": "Bearer operator-secret"}
            )
            assert result.status_code == 200 and result.json() == {"items": [], "has_more": False}


@pytest.mark.asyncio
async def test_http_shutdown_joins_background_runner_before_resources_close(
    database, seed_session, monkeypatch
):
    from contextlib import asynccontextmanager

    from kapy.tmpv2.plugins.http import app as http_app

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
    monkeypatch.setattr(SessionService, "start_runner", runner)
    app = create_app(
        HttpSettings(
            common=CommonSettings(
                database_url=SecretStr(database.url), database_schema=database.schema
            ),
            control_token="operator-secret",
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        async with app.router.lifespan_context(app):
            request = asyncio.create_task(
                client.post(
                    f"/api/sessions/{session_id}/inputs",
                    json={"content": "test"},
                    headers={"Authorization": "Bearer operator-secret"},
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

    from kapy.tmpv2.plugins.telegram import main as telegram_main

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
            database_url=SecretStr(database.url), database_schema=database.schema
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
                "queue",
                "steer",
                "status",
                "cancel",
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
            assert not await self.read_cancel(target)
            events.append("runner-cleaned")

    monkeypatch.setattr(telegram_main, "open_resources", resources)
    monkeypatch.setattr(TelegramClient, "api", api)
    monkeypatch.setattr(SessionService, "start_runner", runner)
    task = asyncio.create_task(telegram_main.serve(settings))
    try:
        await asyncio.wait_for(started.wait(), 3)
    finally:
        task.cancel()
        (result,) = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError)
    assert events == ["runner-cleaned", "resources-closed"]


@pytest.mark.asyncio
async def test_http_plugin_mounts_existing_frontend_with_api(database, tmp_path):
    (tmp_path / "index.html").write_text("<main>existing frontend</main>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app.js").write_text("export {}")
    app = create_app(
        HttpSettings(
            common=CommonSettings(
                database_url=SecretStr(database.url), database_schema=database.schema
            ),
            control_token="operator-secret",
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
            result = await client.get(
                "/api/sessions", headers={"Authorization": "Bearer operator-secret"}
            )
            assert result.status_code == 200
            assert (await client.get("/openapi.json")).json()["paths"]["/api/sessions"]


@pytest.mark.asyncio
async def test_http_plugin_rejects_missing_frontend_entry(tmp_path):
    app = create_app(HttpSettings(control_token="test", frontend_dist=tmp_path))
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
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    from kapy.tmpv2.plugins.telegram import delivery as delivery_module
    from kapy.tmpv2.plugins.telegram.models import DeliveryRow

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

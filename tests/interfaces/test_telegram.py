"""Observable Telegram input/delivery contracts with real private SQLite transactions."""

import asyncio
import json
import time
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx2
import pytest
import pytest_asyncio
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.client import ActivityHandle
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from kapy.agent_history import HistoryMessage
from kapy.agent_output import MessageCommitted, TextDelta
from kapy.control.models import ModelService
from kapy.control.sessions import (
    CreateSession,
    SessionCreationError,
    SessionService,
    UpdateSession,
)
from kapy.interfaces.telegram.client import TelegramClient, TelegramFailure, text_chunk
from kapy.interfaces.telegram.controller import TelegramController
from kapy.interfaces.telegram.delivery import TelegramDelivery
from kapy.interfaces.telegram.models import DeliveryRow
from kapy.interfaces.telegram.repository import TelegramRepository, delivery_key
from kapy.interfaces.telegram.schema import migrate
from kapy.interfaces.telegram.settings import StorageSettings, TelegramSettings
from kapy.interfaces.telegram.storage import open_storage
from kapy.lifecycle import LifecycleStatus
from pydantic_ai_layer_protocol import RetryableScopeError


@pytest_asyncio.fixture
async def repository(tmp_path):
    path = tmp_path / "telegram.sqlite3"
    await migrate(path, "upgrade")
    async with open_storage(path) as engine:
        yield TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))


def telegram_client():
    client = AsyncMock(spec=TelegramClient)
    client.send.return_value = 123
    return client


def settings(tmp_path):
    return TelegramSettings(
        bot_token="secret",
        allowed_chat_ids={123},
        database_path=tmp_path / "state.sqlite3",
        session_template=CreateSession(provider_id=uuid4(), model_name="test"),
    )


def update(update_id, text="hello", *, chat=123, thread=0, **message):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat, "type": "private"},
            "from": {"id": 5, "is_bot": False},
            "text": text,
            "message_thread_id": thread,
            **message,
        },
    }


def controller(repository, tmp_path):
    sessions = AsyncMock(spec=SessionService)
    existing = {}

    async def create(data):
        record = SimpleNamespace(id=data.id, status=LifecycleStatus.READY)
        existing[record.id] = record
        sessions.create_session.return_value = record
        return record

    async def get(session_id):
        if session_id not in existing:
            raise LookupError("Session does not exist")
        return existing[session_id]

    sessions.create_session.side_effect = create
    sessions.get_session.side_effect = get
    sessions.is_runner_running.return_value = False
    client = telegram_client()
    result = TelegramController(
        client=client,
        sessions=sessions,
        models=AsyncMock(spec=ModelService),
        repository=repository,
        settings=settings(tmp_path),
        bot_id=42,
        username="kapy_bot",
    )
    return result, sessions, client


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["queue text", "steer text", "cancel"])
async def test_removed_commands_do_not_create_or_submit_execution(repository, tmp_path, command):
    app, sessions, client = controller(repository, tmp_path)
    await repository.ingest(42, [update(1, f"/{command}")])
    assert await app.process_once()
    sessions.create_session.assert_not_awaited()
    sessions.start_runner.assert_not_awaited()
    assert client.send.await_args.args[-1] == "Unknown command. Use /help."


@pytest.mark.asyncio
async def test_saved_obsolete_action_is_reported_without_allocating_resources(repository, tmp_path):
    app, sessions, client = controller(repository, tmp_path)
    await repository.ingest(42, [update(1, "/queue text")])
    item = await repository.next_inbox(42)
    assert item is not None
    await repository.save_action(item, {"command": "queue", "session_id": None, "text": "text"})
    assert await app.process_once()
    sessions.create_session.assert_not_awaited()
    sessions.start_runner.assert_not_awaited()
    assert client.send.await_args.args[-1] == "Unknown command. Use /help."


def test_xdg_path_resolution_is_at_construction(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("KAPY_TELEGRAM_DATABASE_PATH", raising=False)
    assert (
        StorageSettings().database_path == tmp_path / "xdg/kapy/plugins/telegram/telegram.sqlite3"
    )
    monkeypatch.setenv("XDG_STATE_HOME", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert (
        StorageSettings().database_path
        == tmp_path / "home/.local/state/kapy/plugins/telegram/telegram.sqlite3"
    )
    assert (
        StorageSettings(database_path=tmp_path / "explicit.db").database_path
        == tmp_path / "explicit.db"
    )
    with pytest.raises(ValueError):
        StorageSettings(database_path=Path("relative.db"))


@pytest.mark.asyncio
async def test_sqlite_upgrade_is_idempotent_and_transactions_rollback(tmp_path):
    path = tmp_path / "db.sqlite3"
    await migrate(path, "upgrade")
    await migrate(path, "upgrade")
    async with open_storage(path) as engine:
        async with engine.connect() as db:
            assert await db.scalar(text("PRAGMA journal_mode")) == "wal"
            assert await db.scalar(text("PRAGMA synchronous")) == 2
            assert await db.scalar(text("PRAGMA busy_timeout")) == 5000
            tables = await db.run_sync(lambda connection: inspect(connection).get_table_names())
            assert set(tables) == {
                "plugin_telegram_poll",
                "plugin_telegram_inbox",
                "plugin_telegram_routes",
                "plugin_telegram_delivery",
                "plugin_telegram_defaults",
                "plugin_telegram_schema_version",
            }
        with pytest.raises(RuntimeError):
            async with engine.begin() as db:
                await db.execute(text("CREATE TABLE rollback_test (value INTEGER)"))
                raise RuntimeError("rollback")
        async with engine.connect() as db:
            assert not await db.run_sync(
                lambda connection: inspect(connection).has_table("rollback_test")
            )


@pytest.mark.asyncio
async def test_upgrade_preserves_pending_delivery_from_before_reply_receipts(tmp_path):
    from alembic import command

    from kapy.database.migration import migration_config
    from kapy.interfaces.telegram.models import TelegramTable

    path = tmp_path / "telegram.sqlite3"
    session_id = uuid4()
    pending = {"seq": 4, "text": "pending reply", "format": "plain"}
    async with open_storage(path, create=True) as engine:
        async with engine.begin() as db:

            def previous_revision(connection):
                config = migration_config(
                    connection,
                    directory=Path(__file__).parents[2] / "src/kapy/interfaces/telegram/migrations",
                    metadata=TelegramTable.metadata,
                    version_table="plugin_telegram_schema_version",
                    owns_table=lambda name: name.startswith("plugin_telegram_"),
                )
                command.upgrade(config, "4c7a85e5a4ab")

            await db.run_sync(previous_revision)
            await db.execute(
                text(
                    "INSERT INTO plugin_telegram_delivery "
                    "(bot_id, chat_id, thread_id, session_id, chat_type, after_seq, "
                    "pending, item_offset, next_attempt_at, blocked_error) "
                    "VALUES (42, 123, 9, :session_id, 'private', 2, :pending, 3, 0, NULL)"
                ),
                {"session_id": session_id.hex, "pending": json.dumps(pending)},
            )
    await migrate(path, "upgrade")
    async with open_storage(path) as engine:
        restored = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        row = await restored.get_delivery((42, 123, 9, session_id))
    assert row.after_seq == 2 and row.item_offset == 3 and row.pending == pending


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_seq", [3, 8])
async def test_offline_reply_migration_advances_receipts_and_preserves_unfinished_offset(
    tmp_path, pending_seq
):
    from alembic import command

    from kapy.database.migration import migration_config
    from kapy.interfaces.telegram.models import TelegramTable

    path, session_id = tmp_path / "telegram.sqlite3", uuid4()
    pending = {
        "seq": pending_seq,
        "text": "unfinished reply",
        "format": "plain",
        "authoritative": False,
        "message_ids": [123],
        "item_index": 1,
    }
    async with open_storage(path, create=True) as engine:
        async with engine.begin() as db:

            def config(connection):
                return migration_config(
                    connection,
                    directory=Path(__file__).parents[2] / "src/kapy/interfaces/telegram/migrations",
                    metadata=TelegramTable.metadata,
                    version_table="plugin_telegram_schema_version",
                    owns_table=lambda name: name.startswith("plugin_telegram_"),
                )

            await db.run_sync(
                lambda connection: command.upgrade(config(connection), "18474f847e76")
            )
            await db.execute(
                text(
                    "INSERT INTO plugin_telegram_delivery "
                    "(bot_id, chat_id, thread_id, session_id, chat_type, after_seq, "
                    "sent_responses, "
                    "pending, item_offset, next_attempt_at, blocked_error) "
                    "VALUES (42, 123, 9, :session, 'private', 2, :receipts, :pending, 4, 10, '400')"
                ),
                {
                    "session": session_id.hex,
                    "receipts": json.dumps({"5": {"text": "sent", "message_ids": [90]}}),
                    "pending": json.dumps(pending),
                },
            )
            await db.run_sync(lambda connection: command.upgrade(config(connection), "head"))
            await db.run_sync(lambda connection: command.check(config(connection)))
            row = (await db.execute(text("SELECT * FROM plugin_telegram_delivery"))).one()
            assert row.after_seq == 5
            if pending_seq <= 5:
                assert row.pending is None
                assert (row.item_offset, row.next_attempt_at, row.blocked_error) == (0, 0, None)
            else:
                assert json.loads(row.pending) == {
                    "seq": pending_seq,
                    "text": "unfinished reply",
                    "format": "plain",
                }
                assert (row.item_offset, row.next_attempt_at, row.blocked_error) == (4, 10, "400")
            with pytest.raises(NotImplementedError, match="irreversible"):
                await db.run_sync(
                    lambda connection: command.downgrade(config(connection), "18474f847e76")
                )


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["/new", "hello"])
async def test_unconfigured_model_keeps_bot_available_without_creating_session(
    repository, tmp_path, message
):
    app, sessions, client = controller(repository, tmp_path)
    app.settings = TelegramSettings(
        bot_token="secret", allowed_chat_ids={123}, database_path=tmp_path / "state.sqlite3"
    )
    await repository.ingest(42, [update(1, message), update(2, "/help")])
    assert await app.process_once()
    assert await app.process_once()
    assert "/model <provider UUID> <model name>" in client.send.call_args_list[0].args[2]
    assert "/help" in client.send.call_args_list[1].args[2]
    sessions.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_command_without_session_only_saves_default(repository, tmp_path):
    app, sessions, _ = controller(repository, tmp_path)
    app.settings = app.settings.model_copy(update={"session_template": None})
    provider_id = uuid4()
    await repository.ingest(42, [update(1, f"/model {provider_id} new-model")])
    await app.process_once()
    sessions.update_session.assert_not_awaited()
    sessions.create_session.assert_not_awaited()
    assert await repository.route(42, 123, 0) is None
    await repository.ingest(42, [update(2, "/new")])
    await app.process_once()
    sessions.create_session.assert_awaited_once_with(
        CreateSession(
            id=sessions.create_session.await_args.args[0].id,
            provider_id=provider_id,
            model_name="new-model",
        )
    )


@pytest.mark.asyncio
async def test_model_command_updates_session_and_persists_default_after_restart(
    repository, tmp_path
):
    app, sessions, client = controller(repository, tmp_path)
    await repository.ingest(42, [update(1, "/new", thread=17)])
    await app.process_once()
    target = await repository.route(42, 123, 17)
    provider_id, model_name = uuid4(), "vendor/new-model"
    await repository.ingest(42, [update(2, f"/model {provider_id} {model_name}", thread=17)])
    await app.process_once()
    sessions.update_session.assert_awaited_once_with(
        target, UpdateSession(provider_id=provider_id, model_name=model_name)
    )
    assert "Current session updated" in client.send.call_args_list[-1].args[2]
    assert await repository.route(42, 123, 17) == target

    async with open_storage(tmp_path / "telegram.sqlite3") as engine:
        restored = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        restarted, new_sessions, new_client = controller(restored, tmp_path)
        await restored.ingest(42, [update(3, "/model"), update(4, "/new", thread=18)])
        await restarted.process_once()
        assert str(provider_id) in new_client.send.call_args.args[2]
        assert model_name in new_client.send.call_args.args[2]
        await restarted.process_once()
        new_sessions.create_session.assert_awaited_once_with(
            CreateSession(
                id=new_sessions.create_session.await_args.args[0].id,
                provider_id=provider_id,
                model_name=model_name,
            )
        )
        new_sessions.update_session.assert_not_awaited()
        assert await restored.default_model(99) is None


@pytest.mark.asyncio
async def test_saved_model_preserves_session_template_when_creating_after_restart(
    repository, tmp_path
):
    template = CreateSession(
        provider_id=uuid4(),
        model_name="fallback-model",
        layers={"shell": {"cwd": "/workspace", "base_url": "http://shellctl:8765"}},
        model_settings={"temperature": 0.2},
    )
    provider_id, model_name = uuid4(), "selected-model"
    await repository.set_default_model(42, provider_id, model_name)
    async with open_storage(tmp_path / "telegram.sqlite3") as engine:
        restored = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        app, sessions, _ = controller(restored, tmp_path)
        app.settings = app.settings.model_copy(update={"session_template": template})
        await restored.ingest(42, [update(1, "/new")])
        await app.process_once()
        sessions.create_session.assert_awaited_once_with(
            template.model_copy(
                update={
                    "id": sessions.create_session.await_args.args[0].id,
                    "provider_id": provider_id,
                    "model_name": model_name,
                }
            )
        )
        assert template.model_name == "fallback-model" and template.provider_id != provider_id


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "unknown", "unauthorized", "session-update"])
async def test_failed_model_choice_preserves_previous_default(repository, tmp_path, failure):
    app, sessions, _ = controller(repository, tmp_path)
    models = AsyncMock(spec=ModelService)
    app.models = models
    old_provider, old_model = uuid4(), "old-model"
    await repository.set_default_model(42, old_provider, old_model)
    provider_id = uuid4()
    message = f"/model {provider_id} missing-model"
    chat = 123
    if failure == "malformed":
        message = "/model invalid-uuid model"
    elif failure == "unknown":
        models.get_model.side_effect = LookupError("not configured")
    elif failure == "unauthorized":
        chat = 456
    else:
        await repository.ingest(42, [update(1, "/new")])
        await app.process_once()
        sessions.update_session.side_effect = ValueError("incompatible settings")
    await repository.ingest(42, [update(2, message, chat=chat)])
    await app.process_once()
    default = await repository.default_model(42)
    assert default is not None and (default.provider_id, default.model_name) == (
        old_provider,
        old_model,
    )
    if failure in {"malformed", "unauthorized"}:
        models.get_model.assert_not_awaited()
    if failure != "session-update":
        sessions.update_session.assert_not_awaited()
        sessions.create_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_model_retry_keeps_session_progress_before_saving_default(repository, tmp_path):
    app, sessions, client = controller(repository, tmp_path)
    models = AsyncMock(spec=ModelService)
    app.models = models
    await repository.ingest(42, [update(1, "/new")])
    await app.process_once()
    provider_id = uuid4()
    await repository.ingest(42, [update(2, f"/model {provider_id} new-model")])
    original = repository.set_default_model
    repository.set_default_model = AsyncMock(
        side_effect=OperationalError("save default", {}, Exception("temporary failure"))
    )
    with pytest.raises(OperationalError):
        await app.process_once()
    assert await repository.default_model(42) is None
    repository.set_default_model = original
    client.send.side_effect = TelegramFailure(503)
    await app.process_once()
    item = await repository.next_inbox(42)
    assert item is not None and item.resolved_action is not None
    await repository.save_action(item, item.resolved_action)
    client.send.side_effect = None
    await app.process_once()
    sessions.update_session.assert_awaited_once()
    models.get_model.assert_awaited_once_with(provider_id, "new-model")
    assert await repository.next_inbox(42) is None


@pytest.mark.asyncio
async def test_inbox_dedup_topics_and_session_business_only(repository, tmp_path):
    app, sessions, client = controller(repository, tmp_path)
    batch = [
        update(1),
        update(2, "/providers"),
        update(3, "ignored", chat=456),
        update(4, "/status", thread=1),
    ]
    await repository.ingest(42, batch)
    await repository.ingest(42, batch)
    assert await repository.offset(42) == 5
    for _ in batch:
        assert await app.process_once()
    assert not await app.process_once()
    sessions.create_session.assert_awaited_once()
    sessions.start_runner.assert_awaited_once_with(
        sessions.create_session.return_value.id, user_prompt="hello"
    )
    assert await repository.route(42, 123, 0) == sessions.create_session.return_value.id
    assert await repository.route(42, 123, 1) is None
    assert [call.args[2] for call in client.send.call_args_list] == [
        "Unknown command. Use /help.",
        "No current session. Use /new or send text.",
    ]


@pytest.mark.asyncio
async def test_retry_after_confirmation_does_not_resubmit_or_recreate(repository, tmp_path):
    app, sessions, client = controller(repository, tmp_path)
    await repository.ingest(42, [update(1, "/new first")])
    client.send.side_effect = TelegramFailure(503)
    await app.process_once()
    item = await repository.next_inbox(42)
    assert item is not None and item.resolved_action is not None
    assert item.resolved_action["submitted"]
    await repository.save_action(item, item.resolved_action)
    client.send.side_effect = None
    await app.process_once()
    sessions.create_session.assert_awaited_once()
    sessions.start_runner.assert_awaited_once()
    assert await repository.next_inbox(42) is None


@pytest.mark.asyncio
async def test_bind_failure_after_creation_recovers_saved_identity(
    repository, tmp_path, monkeypatch
):
    app, sessions, _ = controller(repository, tmp_path)
    await repository.ingest(42, [update(1, "/new first")])
    original = repository.bind
    original_create = sessions.create_session.side_effect

    async def create_after_saved_identity(data):
        # Read a fresh SQLite transaction before the first core-side effect.
        item = await repository.next_inbox(42)
        assert item is not None and item.resolved_action is not None
        assert item.resolved_action["creating_session_id"] == str(data.id)
        return await original_create(data)

    sessions.create_session.side_effect = create_after_saved_identity

    async def fail_bind(*args, **kwargs):
        raise OperationalError("SQLite temporarily unavailable", {}, Exception())

    monkeypatch.setattr(repository, "bind", fail_bind)
    with pytest.raises(OperationalError):
        await app.process_once()
    item = await repository.next_inbox(42)
    assert item is not None and item.resolved_action is not None
    created_id = sessions.create_session.await_args.args[0].id
    assert item.resolved_action["creating_session_id"] == str(created_id)
    assert item.resolved_action["session_id"] is None
    assert await repository.route(42, 123, 0) is None
    sessions.start_runner.assert_not_awaited()
    monkeypatch.setattr(repository, "bind", original)
    restarted, _, client = controller(repository, tmp_path)
    restarted.sessions = sessions
    await restarted.process_once()
    assert await repository.route(42, 123, 0) == created_id
    sessions.create_session.assert_awaited_once()
    sessions.start_runner.assert_awaited_once_with(created_id, user_prompt="first")
    assert client.send.await_args.args[-1] == "New session created."
    assert await repository.next_inbox(42) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "phase", [LifecycleStatus.CREATING, LifecycleStatus.CLOSING, LifecycleStatus.CLOSED]
)
async def test_inbox_recovery_reports_unfinished_scope_without_replaying_enter(
    repository, tmp_path, phase
):
    app, sessions, client = controller(repository, tmp_path)
    await repository.ingest(42, [update(1, "/new first")])
    item = await repository.next_inbox(42)
    assert item is not None
    identity = uuid4()
    action = await app.resolve(item)
    action["creating_session_id"] = str(identity)
    await repository.save_action(item, action)
    sessions.get_session.side_effect = None
    sessions.get_session.return_value = SimpleNamespace(id=identity, status=phase)
    await app.process_once()
    await app.process_once()
    sessions.create_session.assert_not_awaited()
    sessions.start_runner.assert_not_awaited()
    assert await repository.route(42, 123, 0) is None
    assert str(identity) in client.send.await_args.args[-1]
    assert f"Status: {phase}" in client.send.await_args.args[-1]


@pytest.mark.asyncio
async def test_stale_binding_is_cleared_without_recreating_session(repository, tmp_path):
    app, sessions, _ = controller(repository, tmp_path)
    await repository.ingest(42, [update(1), update(2, "/status")])
    await app.process_once()
    sessions.get_session.side_effect = LookupError("missing")
    await app.process_once()
    assert await repository.route(42, 123, 0) is None
    assert sessions.create_session.await_count == 1


async def delivery_row(repository, *, text="", chat_type="private", chat_id=123):
    row = DeliveryRow(
        bot_id=42, chat_id=chat_id, thread_id=9, session_id=uuid4(), chat_type=chat_type
    )
    if text:
        row.pending = {"seq": 4, "text": text, "format": "rich"}
    await repository.save_delivery(row)
    return row


@pytest.mark.asyncio
async def test_delivery_fallback_and_consume_resume_offsets_survive_restart(repository, tmp_path):
    body = "😀" * 5000
    row = await delivery_row(repository)
    client = telegram_client()
    sessions = AsyncMock(spec=SessionService)
    sent, cursors = [], []

    async def live(session_id, *, after_seq):
        cursors.append(after_seq)
        yield [MessageCommitted(HistoryMessage(session_id, 4, ModelResponse([TextPart(body)])))]

    async def interrupted_send(chat, thread, content, *, rich):
        if rich:
            raise TelegramFailure(400, rich_content_rejected=True)
        if sent:
            saved = await repository.get_delivery(delivery_key(row))
            assert saved.item_offset == 2000 and saved.after_seq == -1
            raise asyncio.CancelledError()
        sent.append(content)

    sessions.live = live
    client.send.side_effect = interrupted_send
    with pytest.raises(asyncio.CancelledError):
        await TelegramDelivery(client, sessions, repository, 42).consume(row)
    saved = await repository.get_delivery(delivery_key(row))
    assert saved.pending == {"seq": 4, "text": body, "format": "plain"}
    assert saved.item_offset == 2000 and saved.after_seq == -1

    async with open_storage(tmp_path / "telegram.sqlite3") as engine:
        restarted = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))

        async def resumed_send(chat, thread, content, *, rich):
            assert not rich
            assert (await restarted.get_delivery(delivery_key(row))).after_seq == -1
            sent.append(content)

        async def resumed_live(session_id, *, after_seq):
            cursors.append(after_seq)
            assert after_seq == 4 and "".join(sent) == body
            saved = await restarted.get_delivery(delivery_key(row))
            assert saved.pending is None and saved.item_offset == 0
            yield [
                MessageCommitted(
                    HistoryMessage(session_id, 5, ModelRequest([UserPromptPart("next")]))
                )
            ]

        client.send.side_effect = resumed_send
        sessions.live = resumed_live
        await TelegramDelivery(client, sessions, restarted, 42).consume(
            await restarted.get_delivery(delivery_key(row))
        )
        assert (await restarted.get_delivery(delivery_key(row))).after_seq == 5
    assert cursors == [-1, 4]
    assert "".join(sent) == body and [len(chunk) for chunk in sent] == [2000, 2000, 1000]
    assert all(len(chunk.encode("utf-16-le")) <= 8000 for chunk in sent)


@pytest.mark.asyncio
async def test_permanent_send_error_blocks_and_keeps_pending(repository):
    row = await delivery_row(repository, text="hello")
    client = telegram_client()
    client.send.side_effect = TelegramFailure(403)
    delivery = TelegramDelivery(client, AsyncMock(spec=SessionService), repository, 42)
    await delivery.send_pending(row)
    await delivery.send_pending(row)
    client.send.assert_awaited_once()
    saved = await repository.get_delivery(delivery_key(row))
    assert saved.blocked_error == "403" and saved.after_seq == -1 and saved.pending is not None


@pytest.mark.asyncio
async def test_live_deltas_are_temporary_commits_delivered_once_and_close(repository):
    row = await delivery_row(repository)
    client = telegram_client()
    closed = asyncio.Event()
    replayed = asyncio.Event()
    preview_sent = asyncio.Event()

    async def send(chat, thread, content, **kwargs):
        if kwargs.get("draft_id") is not None:
            if content == "new preview":
                preview_sent.set()
        return 123

    client.send.side_effect = send

    async def live(session_id, *, after_seq):
        assert after_seq == -1
        try:
            yield [
                MessageCommitted(
                    HistoryMessage(session_id, 0, ModelRequest([UserPromptPart("input")]))
                ),
                TextDelta(session_id, 1, 0, "text", "replace", "old"),
            ]
            yield [
                TextDelta(session_id, 1, 0, "text", "replace", "new"),
                TextDelta(session_id, 1, 0, "text", "append", " preview"),
            ]
            await preview_sent.wait()
            yield [
                MessageCommitted(
                    HistoryMessage(session_id, 1, ModelResponse([TextPart("complete")]))
                ),
                MessageCommitted(
                    HistoryMessage(session_id, 2, ModelRequest([UserPromptPart("next")]))
                ),
                MessageCommitted(
                    HistoryMessage(session_id, 3, ModelResponse([TextPart("second")]))
                ),
            ]
            replayed.set()
            await asyncio.Future()
        finally:
            closed.set()

    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    delivery = TelegramDelivery(client, sessions, repository, 42)
    task = asyncio.create_task(delivery.consume(row))
    try:
        async with asyncio.timeout(2):
            await replayed.wait()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert closed.is_set()
    calls = client.send.call_args_list
    assert [call.args[2] for call in calls if call.kwargs.get("draft_id") is None] == [
        "complete",
        "second",
    ]
    assert (await repository.get_delivery(delivery_key(row))).after_seq == 3
    assert [call.args[2] for call in calls if call.kwargs.get("draft_id") is not None] == [
        "old",
        "new preview",
    ]


@pytest.mark.asyncio
async def test_thinking_preview_slides_over_latest_five_hundred_characters(repository):
    row = await delivery_row(repository)
    client = telegram_client()

    async def live(session_id, *, after_seq):
        for event in [
            TextDelta(session_id, 0, 0, "thinking", "replace", "思" * 499),
            TextDelta(session_id, 0, 0, "thinking", "append", "😀"),
            TextDelta(session_id, 0, 1, "thinking", "replace", "新"),
            TextDelta(session_id, 0, 1, "thinking", "append", "\n\n段落"),
            TextDelta(session_id, 0, 1, "thinking", "append", "续" * 700),
            TextDelta(session_id, 0, 1, "thinking", "replace", "重置"),
            TextDelta(session_id, 1, 0, "thinking", "replace", "新的回复"),
            TextDelta(session_id, 1, 1, "text", "replace", "answer"),
            TextDelta(session_id, 1, 0, "thinking", "append", "更多思考"),
        ]:
            yield [event]

    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    await TelegramDelivery(client, sessions, repository, 42).consume(row)

    calls = client.send.call_args_list
    assert [call.args[2] for call in calls] == [
        "思" * 499,
        "思" * 499 + "😀",
        "思" * 498 + "😀新",
        "思" * 494 + "😀新\n\n段落",
        "续" * 500,
        "思" * 497 + "😀重置",
        "新的回复",
        "answer",
    ]
    assert all(call.kwargs["draft_id"] == calls[0].kwargs["draft_id"] for call in calls[:6])
    assert all(call.kwargs["rich"] for call in calls)
    assert (await repository.get_delivery(delivery_key(row))).after_seq == -1


@pytest.mark.asyncio
@pytest.mark.parametrize("merged", [True, False])
async def test_thinking_tail_is_independent_of_live_batch_boundaries(repository, merged):
    row = await delivery_row(repository)
    client = telegram_client()
    fragments = [f"{i}:思考\n\n" for i in range(100)]

    async def live(session_id, *, after_seq):
        events = [TextDelta(session_id, 0, 0, "thinking", "append", text) for text in fragments]
        if merged:
            yield events
        else:
            for event in events:
                yield [event]
        for _ in range(51):
            yield []

    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    await TelegramDelivery(client, sessions, repository, 42).consume(row)

    assert client.send.call_args.args[2] == "".join(fragments)[-500:]


@pytest.mark.asyncio
async def test_thinking_draft_falls_back_to_plain_text(repository):
    row = await delivery_row(repository)
    client = telegram_client()
    client.send.side_effect = [TelegramFailure(400, rich_content_rejected=True), None]

    async def live(session_id, *, after_seq):
        yield [
            TextDelta(
                session_id, 0, 0, "thinking", "replace", "一\n\n二\n\n三\n\n四\n\n五\n\n六\n\n七"
            )
        ]

    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    await TelegramDelivery(client, sessions, repository, 42).consume(row)

    calls = client.send.call_args_list
    assert [call.kwargs["rich"] for call in calls] == [True, False]
    assert all(call.args[2] == "一\n\n二\n\n三\n\n四\n\n五\n\n六\n\n七" for call in calls)
    assert calls[0].kwargs["draft_id"] == calls[1].kwargs["draft_id"]


@pytest.mark.asyncio
async def test_connection_failure_resumes_after_last_delivered_commit(repository, monkeypatch):
    row = await delivery_row(repository)
    key = delivery_key(row)
    client = telegram_client()
    cursors, closed = [], []
    recovered = asyncio.Event()

    async def live(session_id, *, after_seq):
        cursors.append(after_seq)
        try:
            if len(cursors) == 1:
                yield [
                    MessageCommitted(
                        HistoryMessage(session_id, 0, ModelResponse([TextPart("first")]))
                    )
                ]
                assert (await repository.get_delivery(key)).after_seq == 0
                raise ValkeyConnectionError("Output subscription connection closed")
            yield [
                MessageCommitted(HistoryMessage(session_id, 1, ModelResponse([TextPart("second")])))
            ]
            recovered.set()
            await asyncio.Future()
        finally:
            closed.append(after_seq)

    monkeypatch.setattr("kapy.interfaces.telegram.delivery.retry_delay", lambda *args: 0)
    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    task = asyncio.create_task(TelegramDelivery(client, sessions, repository, 42).follow(key))
    try:
        await asyncio.wait_for(recovered.wait(), 2)
    finally:
        task.cancel()
        (result,) = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(result, asyncio.CancelledError)
    assert cursors == [-1, 0] and closed == [-1, 0]
    assert [call.args[2] for call in client.send.await_args_list] == ["first", "second"]
    assert (await repository.get_delivery(key)).after_seq == 1


def test_plain_chunks_preserve_unicode():
    body = "x" * 3999 + "😀" + "tail"
    left, right = text_chunk(body)
    assert left + right == body and left == "x" * 3999


@pytest.mark.asyncio
async def test_draft_rate_limit_delays_following_complete_message():
    calls = []

    def response(request):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return httpx2.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
            )
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 123}})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(response)) as http:
        client = TelegramClient(http, "unused", "https://telegram.invalid")
        with pytest.raises(TelegramFailure):
            await client.send(123, 0, "preview", draft_id=1)
        await client.send(123, 0, "final")
        await client.send(123, 0, "next message")
    assert len(calls) == 3
    assert all(end - start >= 0.99 for start, end in pairwise(calls))


@pytest.mark.asyncio
async def test_rich_send_rejection_preserves_plain_fallback():
    def response(request):
        return httpx2.Response(
            400,
            json={"ok": False, "error_code": 400, "description": "RICH_MESSAGE_TEXT_TOO_LONG"},
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(response)) as http:
        client = TelegramClient(http, "unused", "https://telegram.invalid")
        with pytest.raises(TelegramFailure) as error:
            await client.send(123, 0, "reply", rich=True)
    assert error.value.code == 400 and error.value.rich_content_rejected


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_delivery_waits_for_draft_before_reading_and_closes_on_cancel(repository, cancel):
    row = await delivery_row(repository)
    draft_started, release_draft, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    reads = []
    client = telegram_client()

    async def send(chat, thread, content, **kwargs):
        if kwargs.get("draft_id") is not None:
            draft_started.set()
            await release_draft.wait()
        return 123

    client.send.side_effect = send

    async def live(session_id, *, after_seq):
        try:
            reads.append("preview")
            yield [TextDelta(session_id, 0, 0, "text", "replace", "preview")]
            reads.append("final")
            yield [
                MessageCommitted(HistoryMessage(session_id, 0, ModelResponse([TextPart("final")])))
            ]
        finally:
            closed.set()

    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    task = asyncio.create_task(TelegramDelivery(client, sessions, repository, 42).consume(row))
    try:
        async with asyncio.timeout(2):
            await draft_started.wait()
            assert reads == ["preview"]
            assert (await repository.get_delivery(delivery_key(row))).after_seq == -1
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release_draft.set()
                await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert closed.is_set()
    assert reads == (["preview"] if cancel else ["preview", "final"])
    assert (await repository.get_delivery(delivery_key(row))).after_seq == (-1 if cancel else 0)


@pytest.mark.asyncio
async def test_draft_retry_and_plain_fallback_finish_before_next_read(repository, monkeypatch):
    row = await delivery_row(repository)
    client = telegram_client()
    client.send.side_effect = [
        TelegramFailure(429),
        TelegramFailure(400, rich_content_rejected=True),
        None,
    ]
    monkeypatch.setattr("kapy.interfaces.telegram.delivery.retry_delay", lambda error: 0)

    async def live(session_id, *, after_seq):
        yield [TextDelta(session_id, 0, 0, "text", "replace", "preview")]
        assert client.send.await_count == 3
        assert (await repository.get_delivery(delivery_key(row))).after_seq == -1

    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    await TelegramDelivery(client, sessions, repository, 42).consume(row)
    assert [call.kwargs["rich"] for call in client.send.call_args_list] == [True, True, False]
    assert {call.args[2] for call in client.send.call_args_list} == {"preview"}


@pytest.mark.asyncio
async def test_discovery_retry_preserves_existing_followers(repository, monkeypatch):
    first = await delivery_row(repository)
    second = await delivery_row(repository)
    original = repository.deliveries
    discovered_again = asyncio.Event()
    subscribed = set()
    closed = set()
    failures = 0

    async def deliveries(bot_id):
        nonlocal failures
        failures += 1
        if failures == 1:
            return (first,)
        if failures == 2:
            raise OperationalError("SELECT delivery", {}, Exception("database is locked"))
        return await original(bot_id)

    async def live(session_id, *, after_seq):
        assert session_id not in subscribed
        subscribed.add(session_id)
        if session_id == second.session_id:
            discovered_again.set()
        try:
            await asyncio.Future()
            yield  # pragma: no cover - a confirmed idle subscription has no events
        finally:
            closed.add(session_id)

    monkeypatch.setattr(repository, "deliveries", deliveries)
    from kapy.interfaces.telegram import delivery as module

    monkeypatch.setattr(module, "retry_delay", lambda *args: 0)
    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    task = asyncio.create_task(TelegramDelivery(telegram_client(), sessions, repository, 42).run())
    try:
        async with asyncio.timeout(3):
            await discovered_again.wait()
        assert subscribed == {first.session_id, second.session_id} and not closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert closed == subscribed


@pytest.mark.asyncio
async def test_recreated_controller_retries_submission_to_durably_bound_session(
    repository, tmp_path
):
    app, sessions, _ = controller(repository, tmp_path)
    content = "retain this input"
    sessions.start_runner.side_effect = [
        OperationalError("start_runner", {}, Exception("temporary connection failure")),
        Mock(spec=ActivityHandle),
    ]
    await repository.ingest(42, [update(1, "/new " + content, thread=17)])
    with pytest.raises(OperationalError):
        await app.process_once()
    target = await repository.route(42, 123, 17)
    assert target is not None

    async with open_storage(tmp_path / "telegram.sqlite3") as engine:
        restored = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        restarted = TelegramController(
            client=telegram_client(),
            sessions=sessions,
            models=app.models,
            repository=restored,
            settings=settings(tmp_path),
            bot_id=42,
            username="kapy_bot",
        )
        await restarted.process_once()
        assert await restored.route(42, 123, 17) == target
        assert await restored.next_inbox(42) is None
    sessions.create_session.assert_awaited_once()
    # Two attempts are expected: the failed call's business effect may be ambiguous.
    assert [call.args for call in sessions.start_runner.await_args_list] == [
        (target,),
        (target,),
    ]
    assert all(
        call.kwargs == {"user_prompt": content} for call in sessions.start_runner.await_args_list
    )


@pytest.mark.asyncio
async def test_open_group_stream_skips_drafts_while_private_stream_previews(repository):
    group = await delivery_row(repository, chat_type="supergroup", chat_id=-123)
    private = await delivery_row(repository)
    group_ready, private_preview, commit = asyncio.Event(), asyncio.Event(), asyncio.Event()
    client = telegram_client()

    async def send(chat, thread, content, **kwargs):
        if chat == private.chat_id and kwargs.get("draft_id") is not None:
            private_preview.set()
        return 123

    client.send.side_effect = send

    async def live(session_id, *, after_seq):
        name = "group" if session_id == group.session_id else "private"
        yield [TextDelta(session_id, 0, 0, "text", "replace", name + " preview")]
        if session_id == group.session_id:
            group_ready.set()
        await commit.wait()
        yield [
            MessageCommitted(
                HistoryMessage(
                    session_id,
                    0,
                    ModelResponse([TextPart(name + " final")]),
                )
            )
        ]

    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    delivery = TelegramDelivery(client, sessions, repository, 42)
    tasks = [asyncio.create_task(delivery.consume(group))]
    try:
        async with asyncio.timeout(4):
            # The group has consumed its delta before the private positive control starts.
            await group_ready.wait()
            tasks.append(asyncio.create_task(delivery.consume(private)))
            await private_preview.wait()
            commit.set()
            await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    calls = client.send.await_args_list
    drafts = [call for call in calls if call.kwargs.get("draft_id") is not None]
    assert drafts and all(call.args[0] == private.chat_id for call in drafts)
    assert any(call.args[2] == "private preview" for call in drafts)
    assert sorted(
        (call.args[0], call.args[2]) for call in calls if call.kwargs.get("draft_id") is None
    ) == [(-123, "group final"), (123, "private final")]


@pytest.mark.asyncio
async def test_close_reports_cleanup_failure_then_user_retry_succeeds(repository, tmp_path):
    app, sessions, client = controller(repository, tmp_path)
    await repository.ingest(42, [update(1, "/new")])
    assert await app.process_once()
    session_id = await repository.route(42, 123, 0)
    assert session_id is not None
    attempts = []

    async def close(target):
        attempts.append(target)
        if len(attempts) == 1:
            raise RetryableScopeError("resource cleanup failed")

    sessions.close_session.side_effect = close
    client.send.reset_mock()
    await repository.ingest(42, [update(2, "/close")])
    assert await app.process_once()
    assert await app.process_once()
    assert client.send.await_args.args[-1] == "Layer scope operation failed. Use /close to retry."
    assert await repository.next_inbox(42) is None
    await repository.ingest(42, [update(3, "/close")])
    assert await app.process_once()
    assert client.send.await_args.args[-1] == "Session closed."
    assert attempts == [session_id, session_id]
    assert await repository.route(42, 123, 0) == session_id
    sessions.create_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_busy_close_finishes_inbox_without_automatic_retry(repository, tmp_path):
    from kapy.session_lease import SessionBusy

    app, sessions, client = controller(repository, tmp_path)
    await repository.ingest(42, [update(1, "/new")])
    assert await app.process_once()
    sessions.close_session.side_effect = SessionBusy("occupied")
    await repository.ingest(42, [update(2, "/close")])
    assert await app.process_once()
    assert "close has not started" in client.send.await_args.args[-1]
    assert await repository.next_inbox(42) is None
    assert not await app.process_once()
    sessions.close_session.assert_awaited_once()
    sessions.close_session.side_effect = None
    await repository.ingest(42, [update(3, "/close")])
    assert await app.process_once()
    assert client.send.await_args.args[-1] == "Session closed."
    assert sessions.close_session.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["enter", "exit"])
async def test_scope_retry_exhaustion_becomes_explicit_retry_reply(repository, tmp_path, operation):
    app, sessions, client = controller(repository, tmp_path)
    failure = RetryableScopeError("secret upstream information")
    if operation == "enter":

        async def fail(data):
            raise SessionCreationError(data.id) from failure

        sessions.create_session.side_effect = fail
        await repository.ingest(42, [update(1, "/new")])
    else:
        await repository.ingest(42, [update(1, "/new")])
        assert await app.process_once()
        sessions.close_session.side_effect = failure
        await repository.ingest(42, [update(2, "/close")])
    assert await app.process_once()
    assert await app.process_once()
    assert await repository.next_inbox(42) is None
    if operation == "enter":
        identity = sessions.create_session.await_args.args[0].id
        assert str(identity) in client.send.await_args.args[-1]
        assert "Query or close this ID" in client.send.await_args.args[-1]
    else:
        assert (
            client.send.await_args.args[-1] == "Layer scope operation failed. Use /close to retry."
        )
    assert "secret" not in str(client.send.await_args_list)
    if operation == "enter":
        sessions.create_session.assert_awaited_once()
    else:
        sessions.close_session.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_type", ["private", "supergroup"])
async def test_complete_reply_advances_cursor_and_restart_deduplicates(
    repository, tmp_path, chat_type
):
    row = await delivery_row(repository, chat_type=chat_type)
    sessions = AsyncMock(spec=SessionService)
    client = telegram_client()

    async def live(session_id, *, after_seq):
        yield [
            MessageCommitted(HistoryMessage(session_id, 0, ModelRequest([UserPromptPart("input")])))
        ]
        yield [TextDelta(session_id, 1, 0, "text", "replace", "preview")]
        complete = MessageCommitted(
            HistoryMessage(session_id, 1, ModelResponse([TextPart("complete")]))
        )
        yield [complete]
        assert (await repository.get_delivery(delivery_key(row))).after_seq == 1
        yield [complete, TextDelta(session_id, 1, 0, "text", "append", "stale")]

    sessions.live = live
    await TelegramDelivery(client, sessions, repository, 42).consume(row)
    assert [call.args[2] for call in client.send.call_args_list if "draft_id" in call.kwargs] == (
        ["preview"] if chat_type == "private" else []
    )

    async def replay(session_id, *, after_seq):
        assert after_seq == 1
        yield [
            MessageCommitted(HistoryMessage(session_id, 2, ModelRequest([UserPromptPart("next")]))),
            MessageCommitted(HistoryMessage(session_id, 3, ModelResponse([TextPart("second")]))),
        ]

    async with open_storage(tmp_path / "telegram.sqlite3") as engine:
        restarted = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        sessions.live = replay
        await TelegramDelivery(client, sessions, restarted, 42).consume(
            await restarted.get_delivery(delivery_key(row))
        )
        saved = await restarted.get_delivery(delivery_key(row))
    assert saved.after_seq == 3 and saved.pending is None
    assert [
        call.args[2] for call in client.send.call_args_list if "draft_id" not in call.kwargs
    ] == ["complete", "second"]


@pytest.mark.asyncio
async def test_durable_busy_and_ambiguous_start_are_not_retried_as_queued_input(
    repository, tmp_path
):
    from temporalio.service import RPCError, RPCStatusCode

    from kapy.session_lease import SessionBusy

    app, sessions, client = controller(repository, tmp_path)
    await repository.ingest(42, [update(1), update(2, "next")])
    sessions.start_runner.side_effect = [
        SessionBusy("busy"),
        RPCError("secret", RPCStatusCode.DEADLINE_EXCEEDED, b""),
    ]
    await app.process_once()
    await app.process_once()
    # RPC errors are recorded as a reply; following attempts only send that reply.
    await app.process_once()
    assert sessions.start_runner.await_count == 2
    assert "busy" in client.send.call_args_list[0].args[2]
    assert "/status" in client.send.call_args_list[1].args[2]
    assert "secret" not in str(client.send.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("preview_seq", [1, 4])
@pytest.mark.parametrize("complete_kind", ["text", "request", "tool_call", "tool_return"])
async def test_complete_message_clears_covered_previews_and_keeps_future(
    repository, preview_seq, complete_kind
):
    row = await delivery_row(repository)
    sessions = AsyncMock(spec=SessionService)
    client = telegram_client()
    messages = {
        "text": ModelResponse([TextPart("final")]),
        "request": ModelRequest([UserPromptPart("input")]),
        "tool_call": ModelResponse([ToolCallPart("test_tool", {}, "call")]),
        "tool_return": ModelRequest([ToolReturnPart("test_tool", "done", "call")]),
    }
    progress = {"tool_call": "Calling tool: test_tool", "tool_return": "Tool finished: test_tool"}

    async def send(chat_id, thread_id, content, **kwargs):
        if "draft_id" not in kwargs:
            assert (await repository.get_delivery(delivery_key(row))).after_seq == -1
        return 123

    client.send.side_effect = send

    async def live(session_id, *, after_seq):
        yield [TextDelta(session_id, preview_seq, 0, "text", "replace", "preview")]
        assert client.send.call_args.args[2] == "preview"
        client.send.reset_mock()
        # Permit a stale draft to refresh if it was incorrectly retained.
        future_time = time.monotonic() + 21
        from unittest.mock import patch

        with patch("kapy.interfaces.telegram.delivery.time.monotonic", return_value=future_time):
            yield [MessageCommitted(HistoryMessage(session_id, 3, messages[complete_kind]))]
        assert (await repository.get_delivery(delivery_key(row))).after_seq == 3

    sessions.live = live
    await TelegramDelivery(client, sessions, repository, 42).consume(row)
    drafts = [call.args[2] for call in client.send.call_args_list if "draft_id" in call.kwargs]
    assert drafts == (
        ["preview"]
        if preview_seq > 3
        else [progress[complete_kind]]
        if complete_kind in progress
        else []
    )
    assert [
        call.args[2] for call in client.send.call_args_list if "draft_id" not in call.kwargs
    ] == (["final"] if complete_kind == "text" else [])

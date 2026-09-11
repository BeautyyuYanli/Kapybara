"""Observable Telegram input/delivery contracts with real private SQLite transactions."""

import asyncio
import json
import time
from itertools import pairwise
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
import pytest_asyncio
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker

from kapy.tmpv2.agent_runner import HistoryMessage, MessageCommitted, TextDelta
from kapy.tmpv2.control.models import ModelService
from kapy.tmpv2.control.sessions import (
    CreateSession,
    InputSubmission,
    SessionInput,
    SessionService,
    SubmitInput,
    UpdateSession,
)
from kapy.tmpv2.plugins.telegram.client import TelegramClient, TelegramFailure, text_chunk
from kapy.tmpv2.plugins.telegram.controller import TelegramController
from kapy.tmpv2.plugins.telegram.delivery import TelegramDelivery
from kapy.tmpv2.plugins.telegram.models import DeliveryRow
from kapy.tmpv2.plugins.telegram.repository import TelegramRepository, delivery_key
from kapy.tmpv2.plugins.telegram.schema import migrate
from kapy.tmpv2.plugins.telegram.settings import StorageSettings, TelegramSettings
from kapy.tmpv2.plugins.telegram.storage import open_storage


@pytest_asyncio.fixture
async def repository(tmp_path):
    path = tmp_path / "telegram.sqlite3"
    await migrate(path, "upgrade")
    async with open_storage(path) as engine:
        yield TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))


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
    sessions.create_session.return_value = SimpleNamespace(id=uuid4())
    sessions.submit_input.return_value = InputSubmission(
        input=SessionInput(1, "hello"),
        should_start_runner=True,
    )
    sessions.is_runner_running.return_value = False
    sessions.read_inputs.return_value = ()
    client = AsyncMock(spec=TelegramClient)
    scheduled = []
    result = TelegramController(
        client=client,
        sessions=sessions,
        models=AsyncMock(spec=ModelService),
        repository=repository,
        settings=settings(tmp_path),
        bot_id=42,
        username="kapy_bot",
        schedule_runner=scheduled.append,
    )
    return result, sessions, client, scheduled


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
@pytest.mark.parametrize("message", ["/new", "hello"])
async def test_unconfigured_model_keeps_bot_available_without_creating_session(
    repository, tmp_path, message
):
    app, sessions, client, scheduled = controller(repository, tmp_path)
    app.settings = TelegramSettings(
        bot_token="secret", allowed_chat_ids={123}, database_path=tmp_path / "state.sqlite3"
    )
    await repository.ingest(42, [update(1, message), update(2, "/help")])
    assert await app.process_once()
    assert await app.process_once()
    assert "/model <provider UUID> <model name>" in client.send.call_args_list[0].args[2]
    assert "/help" in client.send.call_args_list[1].args[2]
    sessions.create_session.assert_not_awaited()
    assert scheduled == []


@pytest.mark.asyncio
async def test_model_command_without_session_only_saves_default(repository, tmp_path):
    app, sessions, _, scheduled = controller(repository, tmp_path)
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
        CreateSession(provider_id=provider_id, model_name="new-model")
    )
    assert not scheduled


@pytest.mark.asyncio
async def test_model_command_updates_session_and_persists_default_after_restart(
    repository, tmp_path
):
    app, sessions, client, scheduled = controller(repository, tmp_path)
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
    assert not scheduled

    async with open_storage(tmp_path / "telegram.sqlite3") as engine:
        restored = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        restarted, new_sessions, new_client, new_scheduled = controller(restored, tmp_path)
        await restored.ingest(42, [update(3, "/model"), update(4, "/new", thread=18)])
        await restarted.process_once()
        assert str(provider_id) in new_client.send.call_args.args[2]
        assert model_name in new_client.send.call_args.args[2]
        await restarted.process_once()
        new_sessions.create_session.assert_awaited_once_with(
            CreateSession(provider_id=provider_id, model_name=model_name)
        )
        new_sessions.update_session.assert_not_awaited()
        assert not new_scheduled
        assert await restored.default_model(99) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["malformed", "unknown", "unauthorized", "session-update"])
async def test_failed_model_choice_preserves_previous_default(repository, tmp_path, failure):
    app, sessions, _, scheduled = controller(repository, tmp_path)
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
    assert not scheduled


@pytest.mark.asyncio
async def test_model_retry_keeps_session_progress_before_saving_default(repository, tmp_path):
    app, sessions, client, _ = controller(repository, tmp_path)
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
    app, sessions, client, scheduled = controller(repository, tmp_path)
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
    sessions.submit_input.assert_awaited_once()
    assert sessions.submit_input.call_args.args[1].channel == "queued"
    assert scheduled == [sessions.create_session.return_value.id]
    assert await repository.route(42, 123, 0) == scheduled[0]
    assert await repository.route(42, 123, 1) is None
    assert [call.args[2] for call in client.send.call_args_list] == [
        "Unknown command. Use /help.",
        "No current session. Use /new or send text.",
    ]


@pytest.mark.asyncio
async def test_retry_after_confirmation_does_not_resubmit_or_recreate(repository, tmp_path):
    app, sessions, client, scheduled = controller(repository, tmp_path)
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
    sessions.submit_input.assert_awaited_once()
    assert len(scheduled) == 1
    assert await repository.next_inbox(42) is None


@pytest.mark.asyncio
async def test_stale_binding_is_cleared_without_recreating_session(repository, tmp_path):
    app, sessions, _, _ = controller(repository, tmp_path)
    await repository.ingest(42, [update(1), update(2, "/status")])
    await app.process_once()
    sessions.get_session.side_effect = LookupError("missing")
    await app.process_once()
    assert await repository.route(42, 123, 0) is None
    assert sessions.create_session.await_count == 1


@pytest.mark.asyncio
async def test_recovery_schedules_only_idle_sessions_with_pending_input(repository, tmp_path):
    app, sessions, _, scheduled = controller(repository, tmp_path)
    queued, steer, active, empty = (uuid4() for _ in range(4))
    sessions.create_session.side_effect = [
        SimpleNamespace(id=session_id) for session_id in (queued, steer, active, empty)
    ]
    await repository.ingest(42, [update(index, "/new") for index in range(1, 5)])
    for _ in range(4):
        await app.process_once()
    channels = {
        queued: {"queued": (SessionInput(1, "queued input"),), "steer": ()},
        steer: {"queued": (), "steer": (SessionInput(2, "steer input"),)},
        active: {"queued": (SessionInput(3, "busy input"),), "steer": ()},
        empty: {"queued": (), "steer": ()},
    }
    sessions.is_runner_running.side_effect = lambda session_id: session_id == active
    sessions.read_inputs.side_effect = lambda session_id, channel: channels[session_id][channel]
    await app.recover()
    assert set(scheduled) == {queued, steer} and len(scheduled) == 2


async def delivery_row(repository, *, text="", chat_type="private", chat_id=123):
    row = DeliveryRow(
        bot_id=42, chat_id=chat_id, thread_id=9, session_id=uuid4(), chat_type=chat_type
    )
    if text:
        row.pending = {"seq": 4, "text": text, "format": "rich"}
    await repository.save_delivery(row)
    return row


@pytest.mark.asyncio
async def test_delivery_fallback_and_resume_offsets_survive_restart(repository):
    body = "😀" * 5000
    row = await delivery_row(repository, text=body)
    client = AsyncMock(spec=TelegramClient)
    client.send.side_effect = TelegramFailure(400, rich_content_rejected=True)
    delivery = TelegramDelivery(client, AsyncMock(spec=SessionService), repository, 42)
    await delivery.send_pending(row)
    saved = await repository.get_delivery(delivery_key(row))
    assert saved.pending is not None and saved.pending["format"] == "plain"
    client.send.side_effect = None
    await delivery.send_pending(saved)
    assert saved.item_offset == 2000 and saved.after_seq == -1
    saved = await repository.get_delivery(delivery_key(row))
    while saved.pending:
        await delivery.send_pending(saved)
    assert saved.after_seq == 4 and saved.item_offset == 0
    plain = [call.args[2] for call in client.send.call_args_list if not call.kwargs.get("rich")]
    assert "".join(plain) == body
    assert all(len(chunk.encode("utf-16-le")) <= 8000 for chunk in plain)


@pytest.mark.asyncio
async def test_permanent_send_error_blocks_and_keeps_pending(repository):
    row = await delivery_row(repository, text="hello")
    client = AsyncMock(spec=TelegramClient)
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
    client = AsyncMock(spec=TelegramClient)
    closed = asyncio.Event()
    replayed = asyncio.Event()
    preview_sent = asyncio.Event()

    async def send(chat, thread, content, **kwargs):
        if kwargs.get("draft_id") is not None:
            assert content == "new preview"
            preview_sent.set()

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
            yield [TextDelta(session_id, 1, 0, "text", "replace", "new")]
            yield [TextDelta(session_id, 1, 0, "text", "append", " preview")]
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
        "new preview",
    ]


def test_plain_chunks_preserve_unicode():
    body = "x" * 3999 + "😀" + "tail"
    left, right = text_chunk(body)
    assert left + right == body and left == "x" * 3999


@pytest.mark.asyncio
async def test_delta_burst_does_not_queue_paced_drafts_ahead_of_final(repository):
    row = await delivery_row(repository)
    sent = []

    def response(request):
        sent.append((request.url.path.rsplit("/", 1)[1], json.loads(request.content)))
        return httpx2.Response(200, json={"ok": True, "result": {}})

    async def live(session_id, *, after_seq):
        for index in range(5):
            yield [TextDelta(session_id, 0, 0, "text", "append", str(index))]
        yield [MessageCommitted(HistoryMessage(session_id, 0, ModelResponse([TextPart("final")])))]

    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(response)) as http:
        client = TelegramClient(http, "unused", "https://telegram.invalid")
        # The actual client's one-second chat limiter previously delayed this by five seconds.
        async with asyncio.timeout(2):
            await TelegramDelivery(client, sessions, repository, 42).consume(row)
    assert [
        payload["rich_message"]["markdown"]
        for method, payload in sent
        if method == "sendRichMessage"
    ] == ["final"]
    assert (await repository.get_delivery(delivery_key(row))).after_seq == 0


@pytest.mark.asyncio
async def test_draft_rate_limit_delays_following_complete_message():
    calls = []

    def response(request):
        calls.append(time.monotonic())
        if len(calls) == 1:
            return httpx2.Response(
                429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 1}}
            )
        return httpx2.Response(200, json={"ok": True, "result": {}})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(response)) as http:
        client = TelegramClient(http, "unused", "https://telegram.invalid")
        with pytest.raises(TelegramFailure):
            await client.send(123, 0, "preview", draft_id=1)
        await client.send(123, 0, "final")
        await client.send(123, 0, "next message")
    assert len(calls) == 3
    assert all(end - start >= 0.99 for start, end in pairwise(calls))


@pytest.mark.asyncio
async def test_commit_cancels_inflight_draft_before_sending_final(repository):
    row = await delivery_row(repository)
    draft_started, draft_closed = asyncio.Event(), asyncio.Event()
    sent = []
    times = {}

    async def response(request):
        method = request.url.path.rsplit("/", 1)[1]
        if method.endswith("Draft"):
            times["draft_started"] = time.monotonic()
            draft_started.set()
            try:
                await asyncio.Future()
            finally:
                times["draft_closed"] = time.monotonic()
                draft_closed.set()
        assert draft_closed.is_set()
        times["final"] = time.monotonic()
        sent.append(method)
        return httpx2.Response(200, json={"ok": True, "result": {}})

    async def live(session_id, *, after_seq):
        yield [TextDelta(session_id, 0, 0, "text", "replace", "preview")]
        await draft_started.wait()
        yield [MessageCommitted(HistoryMessage(session_id, 0, ModelResponse([TextPart("final")])))]

    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(response)) as http:
        client = TelegramClient(http, "unused", "https://telegram.invalid")
        async with asyncio.timeout(3):
            await TelegramDelivery(client, sessions, repository, 42).consume(row)
    assert sent == ["sendRichMessage"] and draft_closed.is_set()
    assert times["final"] >= times["draft_closed"]
    assert 0.49 <= times["final"] - times["draft_started"] < 0.9


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
    from kapy.tmpv2.plugins.telegram import delivery as module

    monkeypatch.setattr(module, "retry_delay", lambda *args: 0)
    sessions = AsyncMock(spec=SessionService)
    sessions.live = live
    task = asyncio.create_task(
        TelegramDelivery(AsyncMock(spec=TelegramClient), sessions, repository, 42).run()
    )
    try:
        async with asyncio.timeout(3):
            await discovered_again.wait()
        assert subscribed == {first.session_id, second.session_id} and not closed
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert closed == subscribed


@pytest.mark.asyncio
async def test_bound_steer_and_cancel_use_session_business_without_rescheduling(
    repository, tmp_path
):
    app, sessions, client, scheduled = controller(repository, tmp_path)
    await repository.ingest(
        42,
        [
            update(1, "/new", thread=17),
            update(2, "/steer change course", thread=17),
            update(3, "/cancel", thread=17),
        ],
    )
    await app.process_once()
    target = await repository.route(42, 123, 17)
    assert target is not None
    client.send.reset_mock()
    sessions.submit_input.return_value = InputSubmission(
        input=SessionInput(10, "change course"),
        should_start_runner=False,
    )
    await app.process_once()
    sessions.submit_input.assert_awaited_once_with(
        target,
        SubmitInput(content="change course", channel="steer"),
    )
    await app.process_once()
    sessions.request_cancel.assert_awaited_once_with(target)
    client.send.assert_awaited_once_with(123, 17, "Cancellation requested.")
    sessions.create_session.assert_awaited_once()
    assert not scheduled


@pytest.mark.asyncio
async def test_recreated_controller_retries_submission_to_durably_bound_session(
    repository, tmp_path
):
    app, sessions, _, scheduled = controller(repository, tmp_path)
    content = "retain this input"
    sessions.submit_input.side_effect = [
        OperationalError("submit_input", {}, Exception("temporary connection failure")),
        InputSubmission(input=SessionInput(11, content), should_start_runner=True),
    ]
    await repository.ingest(42, [update(1, "/new " + content, thread=17)])
    with pytest.raises(OperationalError):
        await app.process_once()
    target = await repository.route(42, 123, 17)
    assert target is not None and not scheduled

    async with open_storage(tmp_path / "telegram.sqlite3") as engine:
        restored = TelegramRepository(async_sessionmaker(engine, expire_on_commit=False))
        retried_schedule = []
        restarted = TelegramController(
            client=AsyncMock(spec=TelegramClient),
            sessions=sessions,
            models=app.models,
            repository=restored,
            settings=settings(tmp_path),
            bot_id=42,
            username="kapy_bot",
            schedule_runner=retried_schedule.append,
        )
        await restarted.process_once()
        assert await restored.route(42, 123, 17) == target
        assert await restored.next_inbox(42) is None
    sessions.create_session.assert_awaited_once()
    # Two attempts are expected: the failed call's business effect may be ambiguous.
    assert [call.args for call in sessions.submit_input.await_args_list] == [
        (target, SubmitInput(content=content, channel="queued")),
        (target, SubmitInput(content=content, channel="queued")),
    ]
    assert retried_schedule == [target]


@pytest.mark.asyncio
async def test_open_group_stream_skips_drafts_while_private_stream_previews(repository):
    group = await delivery_row(repository, chat_type="supergroup", chat_id=-123)
    private = await delivery_row(repository)
    group_ready, private_preview, commit = asyncio.Event(), asyncio.Event(), asyncio.Event()
    client = AsyncMock(spec=TelegramClient)

    async def send(chat, thread, content, **kwargs):
        if chat == private.chat_id and kwargs.get("draft_id") is not None:
            private_preview.set()

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

import asyncio
import json
import time
from contextlib import asynccontextmanager
from uuid import UUID

import httpx2
import psycopg
import pytest

from kapy.gateway.app import FrontendContext
from kapy.gateway.telegram import TelegramFailure, TelegramFrontend, request_id

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class Bot(TelegramFrontend):
    def __init__(self, gateway):
        super().__init__(FrontendContext(gateway.settings, gateway, gateway.metadata.pool))
        self.sent = []
        self.fail = False

    async def api(self, method, params):
        if self.fail and method == "sendMessage":
            raise TelegramFailure(429, 2)
        self.sent.append((method, params))
        return {"message_id": len(self.sent), "type": "supergroup"}

    async def send(self, chat, thread, text):
        self._chat_ready.clear()
        await super().send(chat, thread, text)


def update(number, text, thread=0, chat=-100):
    return {
        "update_id": number,
        "message": {
            "text": text,
            "chat": {"id": chat, "type": "supergroup"},
            "message_thread_id": thread,
            "from": {"is_bot": False},
        },
    }


async def test_durable_inbox_topic_config_and_idempotent_creation(gateway):
    bot = Bot(gateway)
    updates = [
        update(1, "/machine one", 9),
        update(2, "/instructions saved", 9),
        update(3, "hello", 9),
        update(4, "/machine two", 10),
        update(5, "/new", 10),
        update(6, "ignored", 1, 999),
    ]
    await bot.ingest(updates)
    await bot.ingest(updates)
    for _ in range(5):
        await bot.process_once()
    routes = await gateway.metadata.rows("SELECT * FROM gateway_telegram_routes ORDER BY thread_id")
    assert len(routes) == 2
    assert routes[0]["config"]["config"]["instructions"] == "saved"
    first = routes[0]["session_id"]
    second = routes[1]["session_id"]
    assert first != second
    assert (await gateway.sessions.get_session(first)).machine_ids == ("one",)
    assert (await gateway.sessions.get_session(second)).machine_ids == ("two",)
    await bot.ingest([update(7, "/new", 9)])
    await bot.process_once()
    route = await bot.route(-100, 9)
    assert route["session_id"] != first
    view = await gateway.sessions.get_session(route["session_id"])
    assert view.config["instructions"] == "saved"
    assert all(params["chat_id"] == -100 for method, params in bot.sent if method == "sendMessage")
    poll = await gateway.metadata.rows("SELECT * FROM gateway_telegram_poll")
    assert poll[0]["next_update_id"] == 8
    assert len(await gateway.metadata.rows("SELECT * FROM gateway_telegram_inbox")) == 7


async def test_state_commit_then_reply_failure_retries_original_route_action(gateway):
    bot = Bot(gateway)
    await bot.ingest([update(1, "/machine one"), update(2, "/new")])
    await bot.process_once()
    bot.fail = True
    await bot.process_once()
    sessions = await gateway.sessions.list_sessions()
    assert len(sessions.items) == 1
    inbox = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_inbox WHERE update_id=2"))[
        0
    ]
    assert inbox["handled"] is False
    assert inbox["resolved_action"]["params"]["request_id"] == request_id(12345, 2, "new.create")
    bot = Bot(gateway)
    await bot.handle(inbox)
    assert len((await gateway.sessions.list_sessions()).items) == 1


async def test_delivery_resume_partial_unicode_output(gateway):
    bot = Bot(gateway)
    await bot.ingest([update(1, "/machine one", 7), update(2, "😀" * 5000, 7)])
    await bot.process_once()
    await bot.process_once()
    sid = (await bot.route(-100, 7))["session_id"]
    await gateway.sessions.wait_submission(
        sid, UUID(request_id(12345, 2, "message.create")), wait_seconds=5
    )
    bot.sent.clear()
    await bot.deliver_once()
    row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
    assert row["item_offset"] > 0
    first = bot.sent[0][1]["text"]
    bot = Bot(gateway)
    for _ in range(6):
        await bot.deliver_once()
    rest = "".join(item[1]["text"] for item in bot.sent)
    combined = first + rest
    assert combined.count("😀") == 5000
    assert all(len(item[1]["text"].encode("utf-16-le")) // 2 <= 4000 for item in bot.sent)
    assert all(item[1]["message_thread_id"] == 7 for item in bot.sent)


async def test_poll_retries_uncommitted_batch_and_restarts_at_committed_offset(
    gateway, monkeypatch
):
    context = FrontendContext(gateway.settings, gateway, gateway.metadata.pool)
    bot = TelegramFrontend(context)
    current = bot
    batch = [update(10, "first", 7), update(11, "second", 8)]
    offsets = []
    fail_commit = False
    connection = gateway.metadata.connection

    @asynccontextmanager
    async def interrupted_transaction():
        nonlocal fail_commit
        async with connection() as cursor:
            yield cursor
            if fail_commit:
                fail_commit = False
                # Both inbox writes and the new offset have executed, but neither commits.
                raise psycopg.OperationalError("Injected failure before inbox commit")

    monkeypatch.setattr(gateway.metadata, "connection", interrupted_transaction)

    async def respond(request):
        nonlocal fail_commit
        assert request.url.path.endswith("/getUpdates")
        offsets.append(json.loads(request.content)["offset"])
        if len(offsets) == 1:
            fail_commit = True
            return httpx2.Response(200, json={"ok": True, "result": batch})
        if len(offsets) == 2:
            assert await gateway.metadata.rows("SELECT * FROM gateway_telegram_inbox") == []
            assert await gateway.metadata.rows("SELECT * FROM gateway_telegram_poll") == []
            current.disabled = True  # Stop this polling instance after its successful ingest.
            return httpx2.Response(200, json={"ok": True, "result": batch})
        current.disabled = True
        return httpx2.Response(200, json={"ok": True, "result": []})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        bot.client = client
        async with asyncio.timeout(5):
            await bot.poll()
        assert offsets == [0, 0]
        inbox = await gateway.metadata.rows(
            "SELECT payload FROM gateway_telegram_inbox ORDER BY update_id"
        )
        assert [row["payload"] for row in inbox] == batch
        poll = await gateway.metadata.rows("SELECT next_update_id FROM gateway_telegram_poll")
        assert poll[0]["next_update_id"] == 12

        current = TelegramFrontend(context)
        current.client = client
        await current.poll()
        assert offsets == [0, 0, 12]


async def test_delivery_429_preserves_chunk_and_restarts_after_persisted_deadline(gateway):
    context = FrontendContext(gateway.settings, gateway, gateway.metadata.pool)
    bot = TelegramFrontend(context)
    content = "A" * 3990 + "😀" * 2500 + "tail"
    attempts, delivered = [], []

    def respond(request):
        assert request.url.path.endswith("/sendMessage")
        params = json.loads(request.content)
        if params["text"] != "Settings saved. Instructions apply to new sessions.":
            attempts.append(params["text"])
            if len(attempts) == 2:
                return httpx2.Response(
                    429,
                    json={"ok": False, "error_code": 429, "parameters": {"retry_after": 2}},
                )
            delivered.append(params["text"])
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def delivery():
        return (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        bot.client = client
        await bot.ingest([update(0, "/machine one", 7), update(1, content, 7)])
        await bot.process_once()
        await bot.process_once()
        sid = (await bot.route(-100, 7))["session_id"]
        await gateway.sessions.wait_submission(
            sid, UUID(request_id(12345, 1, "message.create")), wait_seconds=5
        )
        await bot.deliver_once()
        first = await delivery()
        pending = first["projection"]["pending"]
        assert first["item_offset"] > 0
        assert content in pending["text"]

        await bot.deliver_once()
        failed = await delivery()
        assert failed["cursor"] == first["cursor"]
        assert failed["item_offset"] == first["item_offset"]
        assert failed["projection"] == first["projection"]
        assert failed["next_attempt_at"].timestamp() > time.time()
        assert len(attempts) == 2 and len(delivered) == 1

        restored = TelegramFrontend(context)
        restored.client = client
        await restored.deliver_once()
        assert len(attempts) == 2
        await asyncio.sleep(max(0, failed["next_attempt_at"].timestamp() - time.time()) + 0.02)
        await restored.deliver_once()
        assert attempts[2] == attempts[1]
        async with asyncio.timeout(5):
            while "pending" in (await delivery())["projection"]:
                await restored.deliver_once()
        final = await delivery()
        assert final["cursor"] == pending["cursor"]
        assert final["item_offset"] == 0 and final["next_attempt_at"] is None
        assert "".join(delivered) == pending["text"]
        assert all(len(text.encode("utf-16-le")) // 2 <= 4000 for text in delivered)
        completed_attempts = len(attempts)
        await restored.deliver_once()
        assert len(attempts) == completed_attempts


async def install_output(gateway, monkeypatch, records, *, private=True, thread=0):
    """Real durable gateway rows, with a controllable State output feed."""
    from types import SimpleNamespace

    bot = Bot(gateway)
    await bot.ingest([update(1, "/machine one", thread), update(2, "/new", thread)])
    await bot.process_once()
    await bot.process_once()
    if private:
        await gateway.metadata.rows(
            "UPDATE gateway_telegram_inbox SET payload="
            "jsonb_set(payload, '{message,chat,type}', '\"private\"')"
        )
    bot.sent.clear()
    sid = (await bot.route(-100, thread))["session_id"]

    async def read_output(session_id, *, after, limit, wait_seconds):
        start = int(after or 0)
        page = records[start : start + limit]
        return SimpleNamespace(
            items=page,
            next_cursor=str(start + len(page)),
            has_more=start + len(page) < len(records),
        )

    # plain() handles dataclasses; the output page itself stays outside plain().
    monkeypatch.setattr(gateway.sessions, "read_output", read_output)
    return bot, sid


def record(kind, text="", *, message="m", run="r", **data):
    return {"kind": kind, "text": text, "message_id": message, "run_id": run, "data": data}


def feed(records, *items):
    for item in items:
        records.append({**item, "cursor": str(len(records) + 1)})


async def test_private_drafts_restart_refresh_and_final(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records, thread=7)
    feed(records, record("text_delta", text="ignored", part_index=0))
    records[-1]["data"]["text"] = "Hel"
    await bot.deliver_once()
    first = bot.sent[-1][1]
    assert bot.sent[-1][0] == "sendMessageDraft"
    assert first["text"] == "Hel" and first["draft_id"] != 0 and first["message_thread_id"] == 7
    feed(records, record("text_delta", part_index=0))
    records[-1]["data"]["text"] = "lo"
    bot._chat_ready.clear()
    await bot.deliver_once()
    assert bot.sent[-1][1]["text"] == "Hello"
    assert bot.sent[-1][1]["draft_id"] == first["draft_id"]
    restored = Bot(gateway)
    await restored.deliver_once()
    assert restored.sent[-1][1] == bot.sent[-1][1]
    restored._draft_sent[first["draft_id"]] = ("Hello", time.monotonic() - 21)
    restored._chat_ready.clear()
    await restored.deliver_once()
    assert len(restored.sent) == 2
    feed(records, record("model_response", "Hello"), record("final", output="Hello"))
    await restored.deliver_once()
    assert [p["text"] for m, p in restored.sent if m == "sendMessage"] == ["Hello"]
    assert (await gateway.metadata.rows("SELECT item_offset FROM gateway_telegram_delivery"))[0][
        "item_offset"
    ] == 0


async def test_pages_runs_many_messages_retry_and_interrupted(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records, private=False, thread=9)
    feed(records, *(record("tool_call") for _ in range(199)))
    feed(records, record("text_delta"))
    records[-1]["data"]["text"] = "bad"
    await bot.deliver_once()
    assert bot.sent == []
    feed(
        records,
        {
            "kind": "notice",
            "data": {"kind": "attempt_failed", "failed_message_id": "m"},
            "run_id": "r",
        },
    )
    feed(records, *(record("model_response", f"part{i}", message=f"m{i}") for i in range(12)))
    feed(records, record("text_delta", message="unfinished"))
    records[-1]["data"]["text"] = "do not retain"
    feed(
        records,
        record("interrupted"),
        record("model_response", "last", message="last"),
        record("final", output="corrected"),
    )
    terminal = str(len(records))
    feed(
        records,
        record("waiting"),
        record("model_response", "corrected", run="r2"),
        record("final", output="corrected", run="r2"),
    )
    await bot.deliver_once()
    row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
    assert row["cursor"] == terminal
    assert bot.sent[0][1]["text"] == "\n\n".join([*(f"part{i}" for i in range(12)), "corrected"])
    await bot.deliver_once()
    assert [p["text"] for _, p in bot.sent][-1] == "corrected"
    assert all(m == "sendMessage" and p["message_thread_id"] == 9 for m, p in bot.sent)


async def test_draft_400_falls_back_and_error_is_safe(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)
    feed(records, record("text_delta"))
    records[-1]["data"]["text"] = "unconfirmed"
    original = bot.api

    async def api(method, params):
        if method == "sendMessageDraft":
            raise TelegramFailure(400)
        return await original(method, params)

    monkeypatch.setattr(bot, "api", api)
    await bot.deliver_once()
    row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
    assert row["blocked_error"] is None and row["projection"]["draft"]["unavailable"]
    feed(
        records,
        {
            "kind": "error",
            "run_id": "r",
            "text": "SECRET",
            "data": {"kind": "RuntimeError", "message": "SECRET"},
        },
    )
    await bot.deliver_once()
    assert len(bot.sent) == 1
    text = bot.sent[0][1]["text"]
    assert "SECRET" not in text and "unconfirmed" not in text and "RuntimeError" in text


async def test_legacy_projection_guard_preserves_everything(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)
    await gateway.metadata.rows(
        'UPDATE gateway_telegram_delivery SET projection=\'{"messages":{"m":"old"}}\', '
        "item_offset=2"
    )
    before = await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery")
    await bot.deliver_once()
    assert await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery") == before
    assert bot.sent == []


async def test_ack_failure_repeats_unacknowledged_final(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)
    feed(records, record("final", output="one final"))
    original = gateway.metadata.rows
    failed = False

    async def rows(query, params=()):
        nonlocal failed
        if "item_offset=%s" in query and not failed:
            failed = True
            raise psycopg.OperationalError("ack lost")
        return await original(query, params)

    monkeypatch.setattr(gateway.metadata, "rows", rows)
    with pytest.raises(psycopg.OperationalError):
        await bot.deliver_once()
    restored = Bot(gateway)
    await restored.deliver_once()
    assert bot.sent == restored.sent
    assert bot.sent[0][1]["text"] == "one final"


async def test_route_order_backoff_and_waiting_session_release(gateway, monkeypatch):
    from types import SimpleNamespace

    records = []
    bot, old = await install_output(gateway, monkeypatch, records, private=False)
    await bot.ingest([update(3, "/new")])
    await bot.process_once()
    new = (await bot.route(-100, 0))["session_id"]
    outputs = {old: [], new: []}
    feed(outputs[old], record("text_delta"))
    outputs[old][-1]["data"]["text"] = "first"
    feed(outputs[new], record("final", output="second", run="second"))

    async def read(session_id, *, after, limit, wait_seconds):
        start = int(after or 0)
        page = outputs[session_id][start : start + limit]
        return SimpleNamespace(items=page, next_cursor=str(start + len(page)), has_more=False)

    monkeypatch.setattr(gateway.sessions, "read_output", read)
    bot.sent.clear()
    await bot.deliver_once()
    assert bot.sent == []
    feed(outputs[old], record("final", output="first"))
    await gateway.metadata.rows(
        "UPDATE gateway_telegram_delivery SET next_attempt_at=now()+interval '20 seconds' "
        "WHERE session_id=%s",
        (old,),
    )
    await bot.deliver_once()
    assert bot.sent == []
    await gateway.metadata.rows("UPDATE gateway_telegram_delivery SET next_attempt_at=NULL")
    await bot.deliver_once()
    await bot.deliver_once()
    assert [p["text"] for _, p in bot.sent] == ["first", "second"]


async def test_large_body_exceeds_previous_limit_without_loss(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records, private=False)
    body = ("word😀\n\n" * 40000) + "tail"
    feed(records, record("final", output=body))

    # Keep protocol splitting/PG acknowledgement real; avoid rate-limit wall clock in this test.
    async def send(chat, thread, text):
        await bot.api("sendMessage", {"chat_id": chat, "text": text})

    monkeypatch.setattr(bot, "send", send)
    for _ in range(100):
        await bot.deliver_once()
        row = (await gateway.metadata.rows("SELECT projection FROM gateway_telegram_delivery"))[0]
        if "pending" not in row["projection"]:
            break
    assert "".join(p["text"] for _, p in bot.sent) == body
    assert all(len(p["text"].encode("utf-16-le")) // 2 <= 4000 for _, p in bot.sent)

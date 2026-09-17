import asyncio
import json
import time
from contextlib import asynccontextmanager
from uuid import UUID

import httpx2
import psycopg
import pytest
from psycopg.types.json import Jsonb

from kapy.gateway.frontends import FrontendContext
from kapy.gateway.telegram import (
    TelegramFailure,
    TelegramFrontend,
    progress_text,
    project,
    request_id,
)

from .conftest import MODEL_CONFIG, PROVIDER_ID

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class Bot(TelegramFrontend):
    def __init__(self, gateway):
        super().__init__(
            FrontendContext(
                gateway.settings, gateway, gateway.metadata.pool, gateway.metadata.schema
            )
        )
        self.sent = []
        self.fail = False

    async def route(self, chat, thread):
        """Legacy delivery tests use an explicitly preconfigured fixture route."""
        route = await super().route(chat, thread)
        if "provider_id" not in route["config"]:
            route["config"]["provider_id"] = PROVIDER_ID
            route["config"]["config"].update(MODEL_CONFIG)
            await self.metadata.rows(
                "UPDATE gateway_telegram_routes SET config=%s "
                "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s",
                (Jsonb(route["config"]), self.bot_id, chat, thread),
            )
        return route

    async def api(self, method, params):
        if self.fail and method == "sendMessage":
            raise TelegramFailure(429, 2)
        self.sent.append((method, params))
        return {"message_id": len(self.sent), "type": "supergroup"}

    async def send_rich(self, chat, thread, text):
        self._chat_ready.clear()
        await super().send_rich(chat, thread, text)

    async def send(self, chat, thread, text):
        self._chat_ready.clear()
        await super().send(chat, thread, text)


def sent_text(params):
    return params["rich_message"]["markdown"] if "rich_message" in params else params["text"]


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
    await bot.ingest([update(1, "/machine one", 7), update(2, "😀" * 10000, 7)])
    await bot.process_once()
    await bot.process_once()
    sid = (await bot.route(-100, 7))["session_id"]
    status = await gateway.sessions.wait_submission(
        sid, UUID(request_id(12345, 2, "message.create")), wait_seconds=5
    )
    assert status.completion is not None and status.completion.outcome == "completed"
    bot.sent.clear()
    async with asyncio.timeout(5):
        while True:
            await bot.deliver_once()
            row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
            if row["item_offset"] > 0:
                break
    first = sent_text(bot.sent[0][1])
    bot = Bot(gateway)
    for _ in range(12):
        await bot.deliver_once()
    rest = "".join(sent_text(item[1]) for item in bot.sent)
    combined = first + rest
    assert combined.count("😀") == 10000
    assert all(len(sent_text(item[1]).encode("utf-16-le")) // 2 <= 4000 for item in bot.sent)
    assert all(item[1]["message_thread_id"] == 7 for item in bot.sent)


async def test_poll_retries_uncommitted_batch_and_restarts_at_committed_offset(
    gateway, monkeypatch
):
    context = FrontendContext(
        gateway.settings, gateway, gateway.metadata.pool, gateway.metadata.schema
    )
    bot = TelegramFrontend(context)
    current = bot
    batch = [update(10, "first", 7), update(11, "second", 8)]
    offsets = []
    fail_commit = False
    connection = bot.metadata.connection

    @asynccontextmanager
    async def interrupted_transaction():
        nonlocal fail_commit
        async with connection() as cursor:
            yield cursor
            if fail_commit:
                fail_commit = False
                # Both inbox writes and the new offset have executed, but neither commits.
                raise psycopg.OperationalError("Injected failure before inbox commit")

    monkeypatch.setattr(bot.metadata, "connection", interrupted_transaction)

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
    context = FrontendContext(
        gateway.settings, gateway, gateway.metadata.pool, gateway.metadata.schema
    )
    bot = TelegramFrontend(context)
    route = await bot.route(-100, 7)
    route["config"]["config"].update(MODEL_CONFIG)
    await gateway.metadata.rows(
        "UPDATE gateway_telegram_routes SET config=%s WHERE thread_id=7", (Jsonb(route["config"]),)
    )
    content = "A" * 3990 + "😀" * 10000 + "tail"
    attempts, delivered = [], []

    def respond(request):
        assert request.url.path.endswith(("/sendMessage", "/sendRichMessage"))
        params = json.loads(request.content)
        if sent_text(params) != "Settings saved. Instructions apply to new sessions.":
            attempts.append(sent_text(params))
            if len(attempts) == 2:
                return httpx2.Response(
                    429,
                    json={"ok": False, "error_code": 429, "parameters": {"retry_after": 2}},
                )
            delivered.append(sent_text(params))
        return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})

    async def delivery():
        return (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        bot.client = client
        await bot.ingest([update(0, "/machine one", 7), update(1, content, 7)])
        await bot.process_once()
        await bot.process_once()
        sid = (await bot.route(-100, 7))["session_id"]
        status = await gateway.sessions.wait_submission(
            sid, UUID(request_id(12345, 1, "message.create")), wait_seconds=5
        )
        assert status.completion is not None and status.completion.outcome == "completed"
        async with asyncio.timeout(5):
            while True:
                await bot.deliver_once()
                first = await delivery()
                if first["item_offset"] > 0:
                    break
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
    from kapy.state import RecordPage

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
        return RecordPage(
            items=tuple(page),
            next_cursor=str(start + len(page)),
            has_more=start + len(page) < len(records),
        )

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
    feed(records, record("text_delta", part_index=10))
    records[-1]["data"]["text"] = "o"
    await bot.deliver_once()
    first = bot.sent[-1][1]
    assert bot.sent[-1][0] == "sendRichMessageDraft"
    assert sent_text(first) == "o" and first["draft_id"] != 0 and first["message_thread_id"] == 7
    feed(records, record("text_delta", part_index=2))
    records[-1]["data"]["text"] = "l"
    bot._chat_ready.clear()
    await bot.deliver_once()
    assert sent_text(bot.sent[-1][1]) == "lo"
    feed(records, record("text_delta", part_index=0))
    records[-1]["data"]["text"] = "He"
    feed(records, record("text_delta", part_index=2))
    records[-1]["data"]["text"] = "l"
    bot._chat_ready.clear()
    await bot.deliver_once()
    assert sent_text(bot.sent[-1][1]) == "Hello"
    assert bot.sent[-1][1]["draft_id"] == first["draft_id"]
    restored = Bot(gateway)
    await restored.deliver_once()
    assert restored.sent[-1][1] == bot.sent[-1][1]
    restored._draft_sent[first["draft_id"]] = ("Hello", True, time.monotonic() - 21)
    restored._chat_ready.clear()
    await restored.deliver_once()
    assert len(restored.sent) == 2
    feed(records, record("model_response", "Hello"), record("final", output="Hello"))
    await restored.deliver_once()
    assert [sent_text(p) for m, p in restored.sent if m == "sendRichMessage"] == ["Hello"]
    assert (await gateway.metadata.rows("SELECT item_offset FROM gateway_telegram_delivery"))[0][
        "item_offset"
    ] == 0


async def test_progress_replaced_by_first_delta_and_each_message_ends_draft(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records, thread=9)
    feed(
        records,
        record(
            "tool_call",
            name="process_start",
            args={"command": "pwd"},
            tool_call_id="private-call",
            attempt_id="private-attempt",
        ),
    )
    await bot.deliver_once()
    first = bot.sent[-1]
    assert first[0] == "sendMessageDraft"
    assert sent_text(first[1]) == "process_start · calling · pwd"
    assert "private-" not in sent_text(first[1])
    feed(records, record("tool_result", result="/workspace"))
    bot._chat_ready.clear()
    await bot.deliver_once()
    progress = sent_text(bot.sent[-1][1])
    assert progress == "Tool · returned · /workspace"
    assert bot.sent[-1][1]["draft_id"] == first[1]["draft_id"]
    # An empty delta preserves progress; identical nonempty text still switches to rich.
    feed(records, record("text_delta"))
    await bot.deliver_once()
    assert len(bot.sent) == 2
    feed(records, record("text_delta"))
    records[-1]["data"]["text"] = progress
    bot._chat_ready.clear()
    await bot.deliver_once()
    assert bot.sent[-1][0] == "sendRichMessageDraft"
    assert sent_text(bot.sent[-1][1]) == progress
    assert bot.sent[-1][1]["draft_id"] == first[1]["draft_id"]
    feed(records, record("model_response", "The directory is /workspace."))
    await bot.deliver_once()
    assert bot.sent[-1][0] == "sendRichMessage"
    assert sent_text(bot.sent[-1][1]) == "The directory is /workspace."
    row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
    assert "draft" not in row["projection"] and "message" not in row["projection"]
    # Formal ACK is the boundary; later work uses a fresh draft even after restart.
    restored = Bot(gateway)
    feed(records, record("tool_call", name="check", args={"path": "/workspace"}))
    await restored.deliver_once()
    second_id = restored.sent[-1][1]["draft_id"]
    assert restored.sent[-1][0] == "sendMessageDraft" and second_id != first[1]["draft_id"]
    feed(records, record("text_delta", message="second"))
    records[-1]["data"]["text"] = "Done"
    restored._chat_ready.clear()
    await restored.deliver_once()
    assert restored.sent[-1][1]["draft_id"] == second_id
    assert sent_text(restored.sent[-1][1]) == "Done"
    feed(
        records, record("model_response", "Done", message="second"), record("final", output="Done")
    )
    await restored.deliver_once()
    assert restored.sent[-1][0] == "sendRichMessage"
    assert sent_text(restored.sent[-1][1]) == "Done"
    count = len(restored.sent)
    await restored.deliver_once()
    assert len(restored.sent) == count
    assert all(p["message_thread_id"] == 9 for _, p in bot.sent + restored.sent)


async def test_pages_message_boundaries_retries_and_repeated_answers(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)
    feed(records, record("model_response", "checking", message="preamble"))
    feed(records, *(record("tool_result", result=i) for i in range(201)))
    await bot.deliver_once()
    assert bot.sent == [
        ("sendRichMessage", {"chat_id": -100, "rich_message": {"markdown": "checking"}})
    ]
    assert (await gateway.metadata.rows("SELECT cursor FROM gateway_telegram_delivery"))[0][
        "cursor"
    ] == "1"
    await bot.deliver_once()
    assert sent_text(bot.sent[-1][1]) == "Tool · returned · 199"
    await bot.deliver_once()
    assert sent_text(bot.sent[-1][1]) == "Tool · returned · 200"
    first_draft = bot.sent[-1][1]["draft_id"]
    feed(records, record("text_delta"))
    records[-1]["data"]["text"] = "bad partial"
    bot._chat_ready.clear()
    await bot.deliver_once()
    assert sent_text(bot.sent[-1][1]) == "bad partial"
    feed(records, record("notice"))
    records[-1]["data"] = {
        "kind": "attempt_failed",
        "failed_message_id": "m",
        "message": "private traceback",
    }
    bot._chat_ready.clear()
    await bot.deliver_once()
    assert sent_text(bot.sent[-1][1]) == "Retrying this reply…"
    assert bot.sent[-1][1]["draft_id"] == first_draft
    feed(records, record("text_delta", message="resumed"))
    records[-1]["data"]["text"] = "interrupted partial"
    bot._chat_ready.clear()
    await bot.deliver_once()
    feed(records, record("interrupted"))
    bot._chat_ready.clear()
    await bot.deliver_once()
    assert sent_text(bot.sent[-1][1]) == "Resuming this reply…"
    assert bot.sent[-1][1]["draft_id"] == first_draft
    # More than eight messages survive PostgreSQL round trips without retaining a run list.
    feed(records, *(record("model_response", f"part{i}", message=f"m{i}") for i in range(12)))
    formal = ["checking"]
    for _ in range(12):
        restored = Bot(gateway)
        await restored.deliver_once()
        assert len(restored.sent) == 1 and restored.sent[0][0] == "sendRichMessage"
        formal.append(sent_text(restored.sent[0][1]))
    assert formal == ["checking", *(f"part{i}" for i in range(12))]
    feed(
        records,
        record("final", output="part11"),
        record("waiting"),
        record("model_response", "part11", message="new", run="r2"),
        record("model_response", "part11", message="newer", run="r2"),
        record("final", output="corrected", run="r2"),
    )
    restored = Bot(gateway)
    for _ in range(4):
        await restored.deliver_once()
    assert [sent_text(p) for _, p in restored.sent] == ["part11", "part11", "corrected"]


async def test_draft_400_falls_back_and_error_is_safe(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)
    feed(records, record("model_response", "The first check completed.", message="completed"))
    await bot.deliver_once()
    feed(records, record("text_delta"))
    records[-1]["data"]["text"] = "unconfirmed"
    original = bot.api

    async def api(method, params):
        if method == "sendRichMessageDraft":
            raise TelegramFailure(400, rich_content_rejected=True)
        if method == "sendMessageDraft":
            raise TelegramFailure(400)
        return await original(method, params)

    monkeypatch.setattr(bot, "api", api)
    await bot.deliver_once()
    row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
    assert row["blocked_error"] is None and row["projection"]["message"]["plain"]
    bot._chat_ready.clear()
    await bot.deliver_once()
    row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
    assert row["projection"]["draft"]["unavailable"]
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
    assert len(bot.sent) == 2
    assert bot.sent[0][0] == "sendRichMessage"
    assert sent_text(bot.sent[0][1]) == "The first check completed."
    assert bot.sent[1][0] == "sendMessage"
    text = sent_text(bot.sent[1][1])
    assert "The first check completed." not in text
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


async def test_ack_failure_repeats_only_unacknowledged_message(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)
    feed(records, record("model_response", "already acknowledged", message="first"))
    await bot.deliver_once()
    feed(
        records,
        record("model_response", "one message", message="second"),
        record("final", output="one message"),
    )
    original = bot.metadata.rows
    failed = False

    async def rows(query, params=()):
        nonlocal failed
        if "item_offset=%s" in query and not failed:
            failed = True
            raise psycopg.OperationalError("ack lost")
        return await original(query, params)

    monkeypatch.setattr(bot.metadata, "rows", rows)
    with pytest.raises(psycopg.OperationalError):
        await bot.deliver_once()
    restored = Bot(gateway)
    await restored.deliver_once()
    assert bot.sent[1:] == restored.sent
    assert [sent_text(p) for _, p in bot.sent] == ["already acknowledged", "one message"]
    assert sent_text(restored.sent[0][1]) == "one message"
    # Final is not a second copy, including after recovery from a failed ACK.
    await restored.deliver_once()
    assert len(restored.sent) == 1


async def test_progress_preview_is_bounded_and_restored(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)
    feed(records, record("tool_result", result="😀" * 2100, tool_call_id="private-call"))
    await bot.deliver_once()
    method, params = bot.sent[-1]
    assert method == "sendMessageDraft"
    assert len(params["text"]) == 300 and params["text"].endswith("… [truncated]")
    assert "private-call" not in params["text"]
    restored = Bot(gateway)
    await restored.deliver_once()
    assert restored.sent == bot.sent
    feed(records, record("tool_call", summary="Large content is available in history"))
    restored._chat_ready.clear()
    await restored.deliver_once()
    assert restored.sent[-1][1]["draft_id"] == params["draft_id"]
    assert (
        sent_text(restored.sent[-1][1]) == "Tool · calling · Large content is available in history"
    )
    row = (await gateway.metadata.rows("SELECT projection FROM gateway_telegram_delivery"))[0]
    assert "😀" not in json.dumps(row["projection"], ensure_ascii=False)


async def test_thinking_preview_survives_restart_and_yields_to_text_tools_and_recovery(
    gateway, monkeypatch
):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)

    def thinking(text, part=0, message="m"):
        item = record("notice", message=message, part_index=part)
        item["data"].update(kind="thinking_delta", text=text)
        return item

    feed(records, thinking("Initial plan. " + "x" * 1990))
    await bot.deliver_once()
    method, first = bot.sent[-1]
    assert method == "sendMessageDraft" and len(first["text"]) == 2000
    restored = Bot(gateway)
    feed(records, thinking(" Next step."))
    await restored.deliver_once()
    assert restored.sent[-1][1]["draft_id"] == first["draft_id"]
    assert restored.sent[-1][1]["text"] == (first["text"] + " Next step.")[-2000:]
    feed(records, thinking("New part", part=1), thinking("", part=2))
    restored._chat_ready.clear()
    await restored.deliver_once()
    assert restored.sent[-1][1]["text"] == "New part"
    feed(records, record("text_delta"))
    records[-1]["data"]["text"] = "Answer"
    feed(records, thinking("Late thought", part=1))
    restored._chat_ready.clear()
    await restored.deliver_once()
    assert restored.sent[-1][0] == "sendRichMessageDraft"
    assert sent_text(restored.sent[-1][1]) == "Answer"
    feed(records, record("model_response", "Answer"))
    await restored.deliver_once()
    feed(records, thinking("Another thought", message="second"))
    await restored.deliver_once()
    assert restored.sent[-1][1]["draft_id"] != first["draft_id"]
    feed(records, record("tool_call", name="read_media", args={"path": "./plot.png"}))
    restored._chat_ready.clear()
    await restored.deliver_once()
    assert sent_text(restored.sent[-1][1]) == "read_media · calling · ./plot.png"
    feed(records, thinking("Will be interrupted", message="third"), record("interrupted"))
    restored._chat_ready.clear()
    await restored.deliver_once()
    assert sent_text(restored.sent[-1][1]) == "Resuming this reply…"
    feed(records, thinking("Will retry", message="fourth"))
    notice = record("notice", failed_message_id="fourth")
    notice["data"]["kind"] = "attempt_failed"
    feed(records, notice)
    restored._chat_ready.clear()
    await restored.deliver_once()
    assert sent_text(restored.sent[-1][1]) == "Retrying this reply…"
    preview, projection = project([thinking("Empty response"), record("model_response")], {})
    assert preview == "" and "thinking" not in projection
    assert [sent_text(p) for m, p in restored.sent if m == "sendRichMessage"] == ["Answer"]


async def test_tool_summaries_show_actions_and_results_without_payload_dumps():
    call = progress_text(
        "tool_call",
        {
            "name": "custom_edit",
            "args": {
                "path": "file.py",
                "patch": "secret code\n" * 200,
                "session_token": "hidden-token",
                "process_id": "hidden-id",
            },
        },
    )
    assert "custom_edit · calling · file.py" in call and "200 lines" in call
    assert "secret" not in call and "hidden" not in call
    for state, reason, code in [
        ("running", "quiet", None),
        ("running", "timeout", None),
        ("exited", "exited", 1),
        ("exited", "exited", 0),
    ]:
        result = progress_text(
            "tool_result",
            {
                "name": "process_start",
                "result": {
                    "process": {"state": state, "exit_code": code, "process_id": "hidden"},
                    "reason": reason,
                    "output": {
                        "stdout": {
                            "text": "working tree clean\n" + "detail\n" * 100,
                            "reference": {"session_id": "hidden"},
                        }
                    },
                },
            },
        )
        assert state in result and reason in result and "working tree clean" in result
        assert "hidden" not in result and len(result) <= 300 and len(result.splitlines()) <= 3
        assert result.endswith("… [truncated]")
        if code is not None:
            assert f"exit {code}" in result
        else:
            assert "exit " not in result and "success" not in result
    assert "outcome_unknown" in progress_text(
        "tool_result",
        {
            "name": "custom",
            "result": {"error": "outcome_unknown", "message": "private exception"},
        },
    )
    assert "private" not in progress_text(
        "tool_result", {"result": {"error": "machine_error", "message": "private exception"}}
    )
    assert (
        progress_text("tool_result", {"result": ["media payload", "base64"]})
        == "Tool · returned · 2 items"
    )
    assert (
        progress_text("tool_result", {"result": {"data_base64": "secret"}})
        == "Tool · returned · Result with 1 fields"
    )
    assert "encoded content omitted" in progress_text("tool_result", {"result": "A" * 400})


async def test_route_order_backoff_and_waiting_session_release(gateway, monkeypatch):
    from kapy.state import RecordPage

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
        return RecordPage(items=tuple(page), next_cursor=str(start + len(page)), has_more=False)

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
    assert [sent_text(p) for _, p in bot.sent] == ["first", "second"]


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
    assert "".join(sent_text(p) for _, p in bot.sent) == body
    assert all(
        len(sent_text(p).encode("utf-8")) <= 32768
        if m == "sendRichMessage"
        else len(sent_text(p).encode("utf-16-le")) // 2 <= 4000
        for m, p in bot.sent
    )


@pytest.mark.parametrize("last, final", [("last", "last"), ("", "检查结果")])
async def test_group_messages_send_before_final_and_empty_response(
    gateway, monkeypatch, last, final
):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records, private=False)
    feed(
        records,
        record("model_response", "正在检查", message="z"),
        record("model_response", last, message="a"),
    )
    await bot.deliver_once()
    assert [sent_text(p) for _, p in bot.sent] == ["正在检查"]
    restored = Bot(gateway)
    await restored.deliver_once()
    assert [sent_text(p) for _, p in restored.sent] == ([last] if last else [])
    feed(records, record("final", output=final))
    await restored.deliver_once()
    assert [sent_text(p) for _, p in restored.sent] == [final]
    assert all(m == "sendRichMessage" for m, _ in bot.sent + restored.sent)

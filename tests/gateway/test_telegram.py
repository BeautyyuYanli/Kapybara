from uuid import UUID

import pytest

from kapy.gateway.app import FrontendContext
from kapy.gateway.telegram import TelegramFailure, TelegramFrontend, project, request_id, text_chunk

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
        return {"message_id": len(self.sent)}

    async def send(self, chat, thread, text):
        self._chat_ready.clear()
        await super().send(chat, thread, text)


def update(number, text, thread=0, chat=-100):
    return {
        "update_id": number,
        "message": {
            "text": text,
            "chat": {"id": chat},
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
    rest = "".join(item[1]["text"].partition("] ")[2] for item in bot.sent)
    combined = first.partition("] ")[2] + rest
    assert combined.count("😀") == 5000
    assert all(len(item[1]["text"].encode("utf-16-le")) // 2 <= 4000 for item in bot.sent)
    assert all(item[1]["message_thread_id"] == 7 for item in bot.sent)


async def test_projection_replaces_deltas_and_marks_failed_model_attempts():
    rows = [
        {"kind": "text_delta", "message_id": "m1", "data": {"text": "hello"}},
        {"kind": "model_response", "message_id": "m1", "text": "hello world"},
        {"kind": "final", "data": {"output": "hello world"}},
        {"kind": "notice", "data": {"kind": "attempt_failed", "failed_message_id": "m1"}},
    ]
    text, projection = project(rows, {})
    assert text.count("hello") == 1
    assert "attempt failed" in text
    assert projection["failed"] == ["m1"]
    left, right = text_chunk("x" * 3999 + "😀")
    assert left == "x" * 3999 and right == "😀"

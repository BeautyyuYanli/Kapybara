"""Raw Markdown transport and persisted fallback boundaries, using isolated PostgreSQL."""

import json
from contextlib import asynccontextmanager

import httpx2
import pytest

from kapy.gateway.telegram import TelegramFrontend, rich_chunk

from .test_telegram import Bot, feed, install_output, record, sent_text

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@asynccontextmanager
async def transport(bot, respond):
    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        bot.client = client
        bot.api = TelegramFrontend.api.__get__(bot)
        yield


def success():
    return httpx2.Response(200, json={"ok": True, "result": {"message_id": 1}})


async def test_markdown_draft_and_final_are_original_payloads(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records, thread=8)
    markdown = (
        "# Title\n\n**bold** _italic_ [link](https://example.com)\n\n"
        "|a|b|\n|-|-|\n|1|2|\n\n```py\nx = 1"
    )
    feed(records, record("text_delta"))
    records[-1]["data"]["text"] = markdown
    await bot.deliver_once()
    draft = bot.sent[-1]
    assert draft[0] == "sendRichMessageDraft"
    assert draft[1]["rich_message"] == {"markdown": markdown}
    assert "text" not in draft[1] and "parse_mode" not in draft[1]
    feed(records, record("final", output=markdown + "\n```"))
    await bot.deliver_once()
    assert bot.sent[-1] == (
        "sendRichMessage",
        {
            "chat_id": -100,
            "message_thread_id": 8,
            "rich_message": {"markdown": markdown + "\n```"},
        },
    )


@pytest.mark.parametrize("fence", ["```", "~~~~"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
async def test_rich_chunk_does_not_cut_fences_or_tables(fence, newline):
    prefix = "intro" + newline * 2
    code = fence + "python" + newline + ("😀" * 9000) + newline * 2 + fence + newline * 2
    text = prefix + code + "tail"
    assert rich_chunk(text) == (prefix, code + "tail")
    assert rich_chunk(code + "tail") == ("", code + "tail")
    # A fence-like line with a suffix is content, not a closing fence.
    code = fence + newline + "x" + newline + fence + "suffix" + newline * 2 + "x" * 33000
    assert rich_chunk(code) == ("", code)
    table = "| a | b |" + newline + "|---|---|" + newline + ("|😀|x|" + newline) * 5000
    assert rich_chunk(table) == ("", table)
    closed = fence + newline + "x" + newline + fence + "  " + newline * 2
    assert rich_chunk(closed + "z" * 33000) == (closed, "z" * 33000)


async def test_rich_prefix_then_oversized_table_plain_restart_raw_offset(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records, private=False)
    prefix = "# 😀" + "\r\n\r\n"
    table = "| a | b |\r\n|---|---|\r\n" + "|😀|x|\r\n" * 5000
    feed(records, record("final", output=prefix + table))
    await bot.deliver_once()
    row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
    assert bot.sent == [
        ("sendRichMessage", {"chat_id": -100, "rich_message": {"markdown": prefix}})
    ]
    assert row["item_offset"] == len(prefix)
    assert row["projection"]["pending"]["format"] == "rich"
    await bot.deliver_once()
    row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
    assert row["projection"]["pending"]["format"] == "plain"
    assert row["item_offset"] == len(prefix) + len(sent_text(bot.sent[-1][1]))
    restored = Bot(gateway)
    for _ in range(30):
        await restored.deliver_once()
        if (
            "pending"
            not in (
                await gateway.metadata.rows("SELECT projection FROM gateway_telegram_delivery")
            )[0]["projection"]
        ):
            break
    assert all(m == "sendMessage" for m, _ in restored.sent)
    assert "".join(sent_text(p) for _, p in bot.sent + restored.sent) == prefix + table


@pytest.mark.parametrize("draft", [False, True])
async def test_explicit_format_rejection_persists_plain_before_restart(gateway, monkeypatch, draft):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)
    source = "**original** ```" if draft else "**original**" * 1000
    prefix = "" if draft else "😀" * 7000 + "\r\n\r\n"
    feed(records, record("text_delta") if draft else record("final", output=prefix + source))
    if draft:
        records[-1]["data"]["text"] = source
    calls = []
    if draft:
        await bot.deliver_once()
        draft_id = bot.sent[-1][1]["draft_id"]
        bot._draft_sent[draft_id] = (source, -100)
        bot._chat_ready.clear()

    def respond(request):
        calls.append((request.url.path.rsplit("/", 1)[-1], json.loads(request.content)))
        if prefix and len(calls) == 1:
            return success()
        return httpx2.Response(
            400,
            json={
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: can't parse rich message: invalid block SECRET",
            },
        )

    async with transport(bot, respond):
        await bot.deliver_once()
        if prefix:
            await bot.deliver_once()
    row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
    assert row["item_offset"] == len(prefix) and row["blocked_error"] is None
    assert "SECRET" not in json.dumps(row["projection"])
    if draft:
        assert row["projection"]["draft"]["plain"]
    else:
        assert row["projection"]["pending"]["format"] == "plain"
    restored = Bot(gateway)
    await restored.deliver_once()
    assert restored.sent[-1][0] == ("sendMessageDraft" if draft else "sendMessage")
    if draft:
        assert sent_text(restored.sent[-1][1]) == source
    else:
        for _ in range(4):
            await restored.deliver_once()
        assert "".join(sent_text(p) for _, p in restored.sent) == source
        assert all(m == "sendMessage" for m, _ in restored.sent)
    if draft:
        assert restored.sent[-1][1]["draft_id"] == calls[0][1]["draft_id"]
        feed(records, record("final", output=source))
        await restored.deliver_once()
        assert restored.sent[-1][0] == "sendRichMessage"


@pytest.mark.parametrize(
    "status, response, network",
    [
        (
            400,
            {
                "ok": False,
                "error_code": 400,
                "description": "Bad Request: rich messages unavailable",
            },
            False,
        ),
        (
            400,
            {"ok": False, "error_code": 400, "description": "Bad Request: chat not found"},
            False,
        ),
        (429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 4}}, False),
        (500, {"ok": False, "error_code": 400, "description": "can't parse rich message"}, False),
        (400, {"error_code": 400, "description": "can't parse rich message"}, False),
        (401, {"ok": False, "error_code": 401, "description": "Unauthorized"}, False),
        (403, {"ok": False, "error_code": 403, "description": "Forbidden"}, False),
        (503, {}, True),
    ],
)
async def test_unconfirmed_or_nonformat_failures_never_change_format(
    gateway, monkeypatch, status, response, network
):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records, private=False)
    prefix = "😀" * 7000 + "\n\n"
    source = "**raw**" * 1000
    feed(records, record("final", output=prefix + source))
    calls = []

    def respond(request):
        calls.append(request.url.path.rsplit("/", 1)[-1])
        if len(calls) == 1:
            return success()
        if network:
            raise httpx2.ReadError("unknown result")
        return httpx2.Response(status, json=response)

    async with transport(bot, respond):
        await bot.deliver_once()
        await bot.deliver_once()
    row = (await gateway.metadata.rows("SELECT * FROM gateway_telegram_delivery"))[0]
    assert row["projection"]["pending"]["format"] == "rich"
    assert row["item_offset"] == len(prefix)
    assert calls == ["sendRichMessage", "sendRichMessage"]
    # Simulate operator retry for blocked rows and elapsed retry deadline for transient rows.
    await gateway.metadata.rows(
        "UPDATE gateway_telegram_delivery SET blocked_error=NULL,next_attempt_at=NULL"
    )
    restored = Bot(gateway)
    await restored.deliver_once()
    assert restored.sent == [
        ("sendRichMessage", {"chat_id": -100, "rich_message": {"markdown": source}})
    ]


async def test_existing_version_one_pending_without_format_remains_plain(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)
    from psycopg.types.json import Jsonb

    source = "**already sent**\n\n" + "😀" * 3000
    offset = len("**already sent**\n\n")
    await gateway.metadata.rows(
        "UPDATE gateway_telegram_delivery SET projection=%s,item_offset=%s",
        (
            Jsonb(
                {
                    "version": 1,
                    "messages": {},
                    "pending": {
                        "text": source,
                        "cursor": None,
                        "next": {"version": 1, "messages": {}},
                    },
                }
            ),
            offset,
        ),
    )
    await bot.deliver_once()
    restored = Bot(gateway)
    await restored.deliver_once()
    assert all(m == "sendMessage" for m, _ in bot.sent + restored.sent)
    assert "".join(sent_text(p) for _, p in bot.sent + restored.sent) == source[offset:]


async def test_oversized_fence_draft_uses_plain_but_final_can_use_rich(gateway, monkeypatch):
    records = []
    bot, _ = await install_output(gateway, monkeypatch, records)
    source = "```python\n" + "😀" * 9000
    feed(records, record("text_delta"))
    records[-1]["data"]["text"] = source
    await bot.deliver_once()
    method, params = bot.sent[-1]
    assert method == "sendMessageDraft" and source.startswith(params["text"])
    assert len(params["text"].encode("utf-16-le")) // 2 <= 4000
    restored = Bot(gateway)
    await restored.deliver_once()
    assert restored.sent[-1] == bot.sent[-1]
    feed(records, record("final", output="# Done"))
    await restored.deliver_once()
    assert restored.sent[-1] == (
        "sendRichMessage",
        {"chat_id": -100, "rich_message": {"markdown": "# Done"}},
    )

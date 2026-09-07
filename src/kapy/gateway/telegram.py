"""Durable Telegram long polling, route configuration and output replay.

Bot API calls are injectable for tests. Production never logs token-bearing URLs.
"""

import asyncio
import copy
import logging
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx2
import psycopg
from psycopg.types.json import Jsonb

from kapy.rpc import JsonObject, RpcError
from kapy.state import ServiceUnavailable

from .auth import Principal

if TYPE_CHECKING:
    from .app import FrontendContext

logger = logging.getLogger(__name__)
COMMANDS = {
    "new": "Create a session using saved settings",
    "settings": "Show saved settings",
    "model": "Save the model name",
    "machine": "Save the default machine",
    "instructions": "Save instructions for new sessions",
    "steer": "Steer the current run",
    "queue": "Queue input for the next run",
    "status": "Show session status",
    "help": "Show commands",
}


@dataclass
class TelegramFailure(Exception):
    code: int
    retry_after: float = 1


def request_id(bot_id: int, update_id: int, action: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"kapy:telegram:{bot_id}:{update_id}:{action}"))


def retry_delay(error: TelegramFailure, attempt: int = 0) -> float:
    if error.code == 429:
        return max(1, error.retry_after)
    return min(30, 2 ** min(attempt, 5) + random.uniform(0, 1))


def text_chunk(text: str, units: int = 4000) -> tuple[str, str]:
    """Split at Unicode scalar boundaries while respecting Telegram UTF-16 limits."""
    count = 0
    for index, char in enumerate(text):
        width = 2 if ord(char) > 0xFFFF else 1
        if count + width > units:
            boundary = text.rfind("\n\n", 0, index)
            if boundary >= index // 2:
                index = boundary + 2
            return text[:index], text[index:]
        count += width
    return text, ""


def empty_projection() -> dict[str, Any]:
    """Only install over a legacy row after an offline, verified drain."""
    return {"version": 1, "messages": {}}


class ProjectionMigrationRequired(ValueError):
    """An operator must drain the old control before converting its projection."""


def project(records: list[dict[str, Any]], previous: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if previous and previous.get("version") != 1:
        raise ProjectionMigrationRequired(
            "Telegram projection requires offline drain and migration"
        )
    projection = copy.deepcopy(previous or empty_projection())
    messages = projection["messages"]
    for record in records:
        kind = record["kind"]
        data = record.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if record.get("cursor") is not None:
            projection["cursor"] = record["cursor"]
        if kind not in {"text_delta", "model_response", "final", "error", "interrupted", "notice"}:
            continue
        run = record.get("run_id")
        if run is not None:
            projection["run_id"] = run
        key = record.get("message_id") or ""
        if kind in {"text_delta", "model_response"}:
            message = messages.setdefault(key, {"parts": {}, "text": None})
            if kind == "model_response":
                message["text"] = record.get("text", "")
                message["parts"] = {}
            elif message["text"] is None:
                part = str(data.get("part_index", 0))
                message["parts"][part] = message["parts"].get(part, "") + str(data.get("text", ""))
        elif kind == "notice" and data.get("kind") == "attempt_failed":
            failed = data.get("failed_message_id")
            if failed in messages and messages[failed]["text"] is None:
                del messages[failed]
        elif kind in {"interrupted", "error"}:
            for message_id in list(messages):
                if messages[message_id]["text"] is None:
                    del messages[message_id]
        if kind in {"final", "error"}:
            completed = [m["text"] for m in messages.values() if m["text"]]
            if kind == "final":
                final = str(data.get("output", record.get("text", "")))
                # The final result replaces the last response, preserving tool preambles.
                if final:
                    if completed:
                        completed[-1] = final
                    else:
                        completed.append(final)
            else:
                category = data.get("kind", "")
                # State persists an exception class, never its raw message.
                safe = (
                    category
                    if isinstance(category, str) and category.isidentifier() and len(category) <= 80
                    else ""
                )
                completed.append(
                    "Sorry, I couldn’t complete this reply." + (f" ({safe})" if safe else "")
                )
            text = "\n\n".join(completed)
            following = empty_projection()
            if "chat_type" in projection:
                following["chat_type"] = projection["chat_type"]
            projection["pending"] = {
                "text": text,
                "cursor": projection.get("cursor"),
                "next": following,
            }
            break
    preview = "\n\n".join(
        m["text"]
        if m["text"] is not None
        else "".join(m["parts"][part] for part in sorted(m["parts"], key=int))
        for m in messages.values()
    )
    return preview, projection


class TelegramFrontend:
    def __init__(self, context: FrontendContext) -> None:
        self.settings = context.settings
        self.control = context.control
        self.metadata = context.control.metadata
        token = context.settings.telegram_bot_token
        if token is None or context.settings.telegram_chat_id is None:
            raise ValueError("Telegram requires a token and an allowed chat")
        self.token = token.get_secret_value()
        try:
            self.bot_id = int(self.token.partition(":")[0])
        except ValueError:
            raise ValueError("Telegram token requires a numeric bot ID") from None
        self.allowed_chat = context.settings.telegram_chat_id
        self.client: httpx2.AsyncClient
        self.disabled = False
        self._chat_ready: dict[int, float] = {}
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._draft_sent: dict[int, tuple[str, float]] = {}

    async def api(self, method: str, params: dict[str, Any]) -> Any:
        try:
            response = await self.client.post(
                f"{self.settings.telegram_api_base.rstrip('/')}/bot{self.token}/{method}",
                json=params,
            )
            body = response.json()
            if not response.is_success or not body.get("ok"):
                code = body.get("error_code", response.status_code)
                if code == 401:
                    self.disabled = True
                raise TelegramFailure(code, body.get("parameters", {}).get("retry_after", 1))
            return body["result"]
        except httpx2.HTTPError, ValueError:
            raise TelegramFailure(503, random.uniform(1, 3)) from None

    async def send(self, chat: int, thread: int, text: str) -> None:
        await self._send("sendMessage", chat, thread, text)

    async def send_draft(self, chat: int, thread: int, draft_id: int, text: str) -> None:
        await self._send("sendMessageDraft", chat, thread, text, draft_id=draft_id)

    async def _send(self, method: str, chat: int, thread: int, text: str, **extra: Any) -> None:
        lock = self._chat_locks.setdefault(chat, asyncio.Lock())
        async with lock:
            while (delay := self._chat_ready.get(chat, 0) - time.monotonic()) > 0:  # noqa: ASYNC110 - deadline
                await asyncio.sleep(delay)
            params: dict[str, Any] = {"chat_id": chat, "text": text, **extra}
            if thread:
                params["message_thread_id"] = thread
            try:
                await self.api(method, params)
            except TelegramFailure as exc:
                self._chat_ready[chat] = time.monotonic() + retry_delay(exc)
                raise
            self._chat_ready[chat] = time.monotonic() + 1

    async def run(self) -> None:
        async with httpx2.AsyncClient(timeout=40, trust_env=False) as self.client:
            while not self.disabled:
                try:
                    await self.api(
                        "setMyCommands",
                        {
                            "commands": [
                                {"command": command, "description": description}
                                for command, description in COMMANDS.items()
                            ]
                        },
                    )
                    break
                except TelegramFailure as exc:
                    await asyncio.sleep(retry_delay(exc))
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(self.poll())
                tasks.create_task(self.process())
                tasks.create_task(self.deliver())

    async def ingest(self, updates: list[dict[str, Any]]) -> None:
        if not updates:
            return
        async with self.metadata.connection() as conn:
            for update in updates:
                message = update.get("message", {})
                chat = message.get("chat", {}).get("id", 0)
                thread = message.get("message_thread_id", 0)
                await conn.execute(
                    "INSERT INTO gateway_telegram_inbox"
                    "(bot_id,update_id,chat_id,thread_id,payload) VALUES (%s,%s,%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (self.bot_id, update["update_id"], chat, thread, Jsonb(update)),
                )
            await conn.execute(
                "INSERT INTO gateway_telegram_poll VALUES (%s,%s) ON CONFLICT(bot_id) "
                "DO UPDATE SET next_update_id=GREATEST(gateway_telegram_poll.next_update_id,"
                "EXCLUDED.next_update_id)",
                (self.bot_id, max(item["update_id"] for item in updates) + 1),
            )

    async def poll(self) -> None:
        failures = 0
        while not self.disabled:
            try:
                rows = await self.metadata.rows(
                    "SELECT next_update_id FROM gateway_telegram_poll WHERE bot_id=%s",
                    (self.bot_id,),
                )
                offset = rows[0]["next_update_id"] if rows else 0
                updates = await self.api(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": 25,
                        "limit": 100,
                        "allowed_updates": ["message"],
                    },
                )
                await self.ingest(updates)
                failures = 0
            except TelegramFailure as exc:
                await asyncio.sleep(retry_delay(exc, failures))
                failures += 1
            except psycopg.Error, OSError, ServiceUnavailable:
                logger.warning("Telegram inbox storage unavailable; polling will retry")
                await asyncio.sleep(retry_delay(TelegramFailure(503), failures))
                failures += 1

    async def route(self, chat: int, thread: int) -> dict[str, Any]:
        default: JsonObject = {
            "title": "Telegram",
            "machine_ids": [],
            "default_machine_id": None,
            "config": {"model": self.settings.openai_model},
        }
        if len(self.settings.machine_tokens) == 1:
            machine = next(iter(self.settings.machine_tokens))
            default["machine_ids"] = [machine]
            default["default_machine_id"] = machine
        await self.metadata.rows(
            "INSERT INTO gateway_telegram_routes(bot_id,chat_id,thread_id,config) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (self.bot_id, chat, thread, Jsonb(default)),
        )
        rows = await self.metadata.rows(
            "SELECT * FROM gateway_telegram_routes WHERE bot_id=%s AND chat_id=%s AND thread_id=%s",
            (self.bot_id, chat, thread),
        )
        return rows[0]

    async def resolve(self, inbox: dict[str, Any]) -> dict[str, Any]:
        message = inbox["payload"].get("message", {})
        if inbox["chat_id"] != self.allowed_chat or message.get("from", {}).get("is_bot"):
            return {"kind": "ignore"}
        if "text" not in message:
            return {"kind": "reply", "text": "Please send text; media input is not supported."}
        route = await self.route(inbox["chat_id"], inbox["thread_id"])
        text = message["text"]
        command, _, argument = text.partition(" ")
        command = command.split("@")[0] if command.startswith("/") else ""
        sid = str(route["session_id"]) if route["session_id"] else None
        config = copy.deepcopy(route["config"])
        if command == "/help":
            return {
                "kind": "reply",
                "text": "\n".join(
                    f"/{name}: {description}" for name, description in COMMANDS.items()
                ),
            }
        if command == "/settings":
            return {"kind": "reply", "text": str(config)}
        if command == "/status":
            return (
                {"kind": "status", "session_id": sid}
                if sid
                else {
                    "kind": "reply",
                    "text": "No active session. Use /new or send text.",
                }
            )
        if command in {"/model", "/machine", "/instructions"}:
            if not argument.strip():
                return {"kind": "reply", "text": f"Usage: {command} <value>"}
            if command == "/machine":
                if argument not in self.settings.machine_tokens:
                    return {"kind": "reply", "text": "Unknown configured machine."}
                config["machine_ids"] = [argument]
                config["default_machine_id"] = argument
            else:
                config["config"][command[1:]] = argument
            update = False
            if sid and command != "/instructions":
                view = await self.control.sessions.get_session(UUID(sid))
                update = view.status == "waiting"
            return {
                "kind": "config",
                "config": config,
                "session_id": sid,
                "update": update,
                "request_id": request_id(self.bot_id, inbox["update_id"], "settings.update"),
            }
        if command and command not in {"/new", "/queue", "/steer"}:
            return {"kind": "reply", "text": "Unknown command. Use /help."}
        if command in {"/queue", "/steer"}:
            text = argument
            if not text:
                return {"kind": "reply", "text": f"Usage: {command} <text>"}
        mode = "steer" if command == "/steer" else "queue"
        if command == "/new" or sid is None:
            if not config["machine_ids"]:
                return {"kind": "reply", "text": "Choose a machine with /machine <id> first."}
            return {
                "kind": "create",
                "params": {
                    **config,
                    "input": (None if command == "/new" else text),
                    "mode": mode,
                    "request_id": request_id(
                        self.bot_id,
                        inbox["update_id"],
                        "new.create" if command == "/new" else "message.create",
                    ),
                },
            }
        return {
            "kind": "input",
            "params": {
                "session_id": sid,
                "payload": text,
                "mode": mode,
                "request_id": request_id(self.bot_id, inbox["update_id"], "message.input"),
            },
        }

    async def handle(self, inbox: dict[str, Any]) -> None:
        chat, thread = inbox["chat_id"], inbox["thread_id"]
        action = inbox["resolved_action"]
        if action is None:
            action = await self.resolve(inbox)
            await self.metadata.rows(
                "UPDATE gateway_telegram_inbox SET resolved_action=%s "
                "WHERE bot_id=%s AND update_id=%s",
                (Jsonb(action), self.bot_id, inbox["update_id"]),
            )
        principal = Principal("telegram", telegram_route=(self.bot_id, chat, thread))
        kind = action["kind"]
        reply = None
        if kind == "reply":
            reply = action["text"]
        elif kind in {"create", "input"}:
            result = cast(
                dict[str, Any],
                await self.control.call(
                    "session." + kind,
                    action["params"],
                    principal=principal,
                ),
            )
            if kind == "create":
                sid = result["session"]["id"]
                async with self.metadata.connection() as conn:
                    await conn.execute(
                        "UPDATE gateway_telegram_routes SET session_id=%s "
                        "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s",
                        (sid, self.bot_id, chat, thread),
                    )
                    await conn.execute(
                        "INSERT INTO gateway_telegram_delivery"
                        "(bot_id,chat_id,thread_id,session_id) VALUES (%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING",
                        (self.bot_id, chat, thread, sid),
                    )
                reply = (
                    "Ready for a new conversation."
                    if action["params"].get("input") is None
                    else None
                )
        elif kind == "config":
            await self.metadata.rows(
                "UPDATE gateway_telegram_routes SET config=%s "
                "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s",
                (Jsonb(action["config"]), self.bot_id, chat, thread),
            )
            if action["update"]:
                try:
                    await self.control.call(
                        "session.update",
                        {
                            **action["config"],
                            "session_id": action["session_id"],
                            "request_id": action["request_id"],
                        },
                        principal=principal,
                    )
                except RpcError as exc:
                    if exc.code != -32009:
                        raise
                    reply = "Settings saved for /new; the active session is busy."
            reply = reply or "Settings saved. Instructions apply to new sessions."
        elif kind == "status":
            result = cast(
                dict[str, Any],
                await self.control.call(
                    "session.get",
                    {"session_id": action["session_id"]},
                    principal=principal,
                ),
            )
            reply = result["status"]
        if reply:
            remaining = reply
            while remaining:
                chunk, remaining = text_chunk(remaining)
                await self.send(chat, thread, chunk)
        async with self.metadata.connection() as conn:
            await conn.execute(
                "UPDATE gateway_telegram_inbox SET handled=true WHERE bot_id=%s AND update_id=%s",
                (self.bot_id, inbox["update_id"]),
            )
            if kind != "ignore":
                await conn.execute(
                    "UPDATE gateway_telegram_delivery SET blocked_error=NULL,next_attempt_at=NULL "
                    "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s",
                    (self.bot_id, chat, thread),
                )

    async def process_once(self) -> None:
        rows = await self.metadata.rows(
            "SELECT DISTINCT ON (chat_id,thread_id) * FROM gateway_telegram_inbox "
            "WHERE bot_id=%s AND NOT handled ORDER BY chat_id,thread_id,update_id",
            (self.bot_id,),
        )
        for inbox in rows:
            if (
                inbox["next_attempt_at"] is not None
                and inbox["next_attempt_at"].timestamp() > time.time()
            ):
                continue
            try:
                await self.handle(inbox)
            except TelegramFailure as exc:
                if exc.code in {400, 403}:
                    await self.metadata.rows(
                        "UPDATE gateway_telegram_inbox SET handled=true "
                        "WHERE bot_id=%s AND update_id=%s",
                        (self.bot_id, inbox["update_id"]),
                    )
                else:
                    await self.metadata.rows(
                        "UPDATE gateway_telegram_inbox "
                        "SET next_attempt_at=now()+%s*interval '1 second' "
                        "WHERE bot_id=%s AND update_id=%s",
                        (retry_delay(exc), self.bot_id, inbox["update_id"]),
                    )
            except RpcError as exc:
                if exc.code in {-32602, -32001, -32004, -32009, -32020}:
                    # Persist a safe reply action, then resume its normal send/retry path.
                    await self.metadata.rows(
                        "UPDATE gateway_telegram_inbox SET resolved_action=%s "
                        "WHERE bot_id=%s AND update_id=%s",
                        (
                            Jsonb({"kind": "reply", "text": exc.message}),
                            self.bot_id,
                            inbox["update_id"],
                        ),
                    )
                else:
                    await self.metadata.rows(
                        "UPDATE gateway_telegram_inbox "
                        "SET next_attempt_at=now()+interval '2 seconds' "
                        "WHERE bot_id=%s AND update_id=%s",
                        (self.bot_id, inbox["update_id"]),
                    )

    async def process(self) -> None:
        failures = 0
        while not self.disabled:
            try:
                await self.process_once()
                failures = 0
            except psycopg.Error, OSError, ServiceUnavailable:
                logger.warning("Telegram processing storage unavailable; processing will retry")
                await asyncio.sleep(retry_delay(TelegramFailure(503), failures))
                failures += 1
            await asyncio.sleep(0.25)

    async def deliver_once(self) -> None:
        rows = await self.metadata.rows(
            "SELECT d.*, origin.update_id FROM gateway_telegram_delivery d "
            "LEFT JOIN LATERAL (SELECT min(i.update_id) AS update_id "
            "FROM gateway_telegram_inbox i JOIN gateway_requests r "
            "ON r.request_id::text=i.resolved_action->'params'->>'request_id' "
            "WHERE i.bot_id=d.bot_id AND r.target_session_id=d.session_id "
            "AND i.resolved_action->>'kind'='create') origin ON true "
            "WHERE d.bot_id=%s ORDER BY d.chat_id,d.thread_id,"
            "origin.update_id NULLS FIRST,d.session_id",
            (self.bot_id,),
        )
        busy: set[tuple[int, int]] = set()
        for row in rows:
            route = (row["chat_id"], row["thread_id"])
            if route in busy:
                continue
            # Include blocked/backoff rows in ordering: later replies cannot overtake them.
            if row["blocked_error"] or (
                row["next_attempt_at"] and row["next_attempt_at"].timestamp() > time.time()
            ):
                busy.add(route)
                continue
            try:
                if await self._deliver_row(row):
                    busy.add(route)
            except ProjectionMigrationRequired:
                logger.error("Telegram projection requires offline drain and migration")
                busy.add(route)

    async def _deliver_row(self, row: dict[str, Any]) -> bool:
        from kapy.state import NotFound

        from .control import plain

        key = (self.bot_id, row["chat_id"], row["thread_id"], row["session_id"])
        projection = row["projection"]
        if projection and projection.get("version") != 1:
            raise ProjectionMigrationRequired(
                "Telegram projection requires offline drain and migration"
            )
        projection = copy.deepcopy(projection or empty_projection())
        offset = row["item_offset"]
        pending = projection.get("pending")
        cursor = row["cursor"]
        has_more = False
        try:
            if pending is None:
                page = await self.control.sessions.read_output(
                    row["session_id"], after=cursor, limit=200, wait_seconds=0
                )
                preview, projection = project(
                    cast(list[dict[str, Any]], plain(page.items)), projection
                )
                cursor = projection.pop("cursor", page.next_cursor)
                pending = projection.get("pending")
                has_more = page.has_more or cursor != page.next_cursor
                if pending is None and preview and "draft" not in projection:
                    projection["draft"] = {"id": uuid4().int % (2**63 - 1) + 1}
                await self.metadata.rows(
                    "UPDATE gateway_telegram_delivery SET cursor=%s,projection=%s "
                    "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s AND session_id=%s",
                    (cursor, Jsonb(projection), *key),
                )
            else:
                preview = ""
            if pending is not None:
                chunk, remainder = text_chunk(pending["text"][offset:])
                if chunk:
                    await self.send(row["chat_id"], row["thread_id"], chunk)
                await self.metadata.rows(
                    "UPDATE gateway_telegram_delivery SET cursor=%s,projection=%s,item_offset=%s,"
                    "next_attempt_at=NULL WHERE bot_id=%s AND chat_id=%s "
                    "AND thread_id=%s AND session_id=%s",
                    (
                        cursor,
                        Jsonb(projection if remainder else pending["next"]),
                        offset + len(chunk) if remainder else 0,
                        *key,
                    ),
                )
                if not remainder and (draft := projection.get("draft")):
                    self._draft_sent.pop(draft["id"], None)
                # Revisit this route next tick, including any remaining records on this page.
                return True
            draft = projection.get("draft")
            if draft and not draft.get("unavailable"):
                if "chat_type" not in projection:
                    chat_rows = await self.metadata.rows(
                        "SELECT payload->'message'->'chat'->>'type' AS type "
                        "FROM gateway_telegram_inbox "
                        "WHERE bot_id=%s AND chat_id=%s "
                        "AND payload->'message'->'chat'->>'type' IS NOT NULL "
                        "ORDER BY update_id DESC LIMIT 1",
                        (self.bot_id, row["chat_id"]),
                    )
                    projection["chat_type"] = (
                        chat_rows[0]["type"]
                        if chat_rows
                        else (await self.api("getChat", {"chat_id": row["chat_id"]}))["type"]
                    )
                if projection["chat_type"] == "private":
                    text = text_chunk(preview)[0]
                    sent = self._draft_sent.get(draft["id"])
                    if sent is None or sent[0] != text or time.monotonic() - sent[1] >= 20:
                        try:
                            await self.send_draft(
                                row["chat_id"], row["thread_id"], draft["id"], text
                            )
                            self._draft_sent[draft["id"]] = (text, time.monotonic())
                        except TelegramFailure as exc:
                            if exc.code != 400:
                                raise
                            draft["unavailable"] = True
                await self.metadata.rows(
                    "UPDATE gateway_telegram_delivery SET projection=%s WHERE bot_id=%s "
                    "AND chat_id=%s AND thread_id=%s AND session_id=%s",
                    (Jsonb(projection), *key),
                )
            view = await self.control.sessions.get_session(row["session_id"])
            return has_more or bool(projection.get("run_id")) or view.status != "waiting"
        except NotFound:
            return False
        except TelegramFailure as exc:
            await self.metadata.rows(
                "UPDATE gateway_telegram_delivery SET blocked_error=%s, "
                "next_attempt_at=now()+%s*interval '1 second' "
                "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s AND session_id=%s",
                (str(exc.code) if exc.code in {400, 403} else None, retry_delay(exc), *key),
            )
            return True

    async def deliver(self) -> None:
        failures = 0
        while not self.disabled:
            try:
                await self.deliver_once()
                failures = 0
            except psycopg.Error, OSError, ServiceUnavailable:
                logger.warning("Telegram delivery storage unavailable; delivery will retry")
                await asyncio.sleep(retry_delay(TelegramFailure(503), failures))
                failures += 1
            await asyncio.sleep(1)

"""Durable Telegram long polling, route configuration and output replay.

Bot API calls are injectable for tests. Production never logs token-bearing URLs.
"""

import asyncio
import copy
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import httpx2
from psycopg.types.json import Jsonb

from kapy.rpc import JsonObject, RpcError

from .auth import Principal

if TYPE_CHECKING:
    from .app import FrontendContext

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
            return text[:index], text[index:]
        count += width
    return text, ""


def project(records: list[dict[str, Any]], previous: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    projection = copy.deepcopy(previous)
    messages = projection.setdefault("messages", {})
    output: list[str] = []
    for record in records:
        kind = record["kind"]
        data = record.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        key = record.get("message_id") or ""
        text = record.get("text", "")
        if kind == "text_delta":
            text = str(data.get("text", text))
            messages[key] = messages.get(key, "") + text
            output.append(text)
        elif kind == "model_response":
            old = messages.get(key, "")
            if text.startswith(old):
                output.append(text[len(old) :])
            elif text != old:
                output.append("\n[Corrected response]\n" + text)
            messages[key] = text
            projection["last_response"] = text
        elif kind == "final":
            final = data.get("output", text)
            if final and final != projection.get("last_response"):
                output.append("\n" + final)
            projection["last_response"] = final
        elif kind == "notice" and data.get("kind") == "attempt_failed":
            output.append("\n[Model attempt failed; retrying]\n")
            failed = projection.setdefault("failed", [])
            failed.append(data.get("failed_message_id"))
            projection["failed"] = failed[-32:]
        elif kind in {"waiting", "error", "interrupted"}:
            output.append("\n[" + kind + "]" + (" " + text if text else "") + "\n")
        elif kind == "tool_call":
            output.append("\n[Tool: " + str(data.get("name", "running")) + "]\n")
        elif kind == "notice" and text:
            output.append("\n" + text + "\n")
        while len(messages) > 8:
            del messages[next(iter(messages))]
    return "".join(output), projection


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
        delay = self._chat_ready.get(chat, 0) - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)
        params: dict[str, Any] = {"chat_id": chat, "text": text}
        if thread:
            params["message_thread_id"] = thread
        try:
            await self.api("sendMessage", params)
        except TelegramFailure as exc:
            self._chat_ready[chat] = time.monotonic() + max(1, exc.retry_after)
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
            rows = await self.metadata.rows(
                "SELECT next_update_id FROM gateway_telegram_poll WHERE bot_id=%s",
                (self.bot_id,),
            )
            offset = rows[0]["next_update_id"] if rows else 0
            try:
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
                update = view.status == "waiting" and view.run_id is None
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
                reply = "Session " + sid[:8] + " created."
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
                if exc.code in {-32602, -32001, -32004, -32009}:
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
        while not self.disabled:
            await self.process_once()
            await asyncio.sleep(0.25)

    async def deliver_once(self) -> None:
        rows = await self.metadata.rows(
            "SELECT * FROM gateway_telegram_delivery WHERE bot_id=%s AND blocked_error IS NULL "
            "AND (next_attempt_at IS NULL OR next_attempt_at <= now())",
            (self.bot_id,),
        )
        for row in rows:
            key = (self.bot_id, row["chat_id"], row["thread_id"], row["session_id"])
            projection = row["projection"]
            pending = projection.get("pending")
            if pending is None:
                try:
                    page = await self.control.sessions.read_output(
                        row["session_id"],
                        after=row["cursor"],
                        limit=200,
                        wait_seconds=0,
                    )
                except Exception as exc:
                    from kapy.state import NotFound

                    if not isinstance(exc, NotFound):
                        raise
                    continue
                from .control import plain

                text, updated = project(cast(list[dict[str, Any]], plain(page.items)), projection)
                if not text:
                    await self.metadata.rows(
                        "UPDATE gateway_telegram_delivery SET cursor=%s,projection=%s "
                        "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s AND session_id=%s",
                        (page.next_cursor, Jsonb(updated), *key),
                    )
                    continue
                pending = {"text": text, "offset": 0, "cursor": page.next_cursor, "next": updated}
                projection["pending"] = pending
                await self.metadata.rows(
                    "UPDATE gateway_telegram_delivery SET projection=%s "
                    "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s AND session_id=%s",
                    (Jsonb(projection), *key),
                )
            prefix = f"[{str(row['session_id'])[:8]}] "
            chunk, remainder = text_chunk(pending["text"][pending["offset"] :], 4000 - len(prefix))
            try:
                await self.send(row["chat_id"], row["thread_id"], prefix + chunk)
            except TelegramFailure as exc:
                await self.metadata.rows(
                    "UPDATE gateway_telegram_delivery SET blocked_error=%s, "
                    "next_attempt_at=now()+%s*interval '1 second' "
                    "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s AND session_id=%s",
                    (
                        str(exc.code) if exc.code in {400, 403} else None,
                        retry_delay(exc),
                        *key,
                    ),
                )
                continue
            pending["offset"] += len(chunk)
            await self.metadata.rows(
                "UPDATE gateway_telegram_delivery SET cursor=%s,projection=%s,item_offset=%s,"
                "next_attempt_at=NULL WHERE bot_id=%s AND chat_id=%s "
                "AND thread_id=%s AND session_id=%s",
                (
                    row["cursor"] if remainder else pending["cursor"],
                    Jsonb(projection if remainder else pending["next"]),
                    pending["offset"] if remainder else 0,
                    *key,
                ),
            )

    async def deliver(self) -> None:
        while not self.disabled:
            await self.deliver_once()
            await asyncio.sleep(1)

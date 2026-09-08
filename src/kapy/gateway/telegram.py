"""Durable Telegram long polling, route configuration and output replay.

Bot API calls are injectable for tests. Production never logs token-bearing URLs.
"""

import asyncio
import copy
import hashlib
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx2
import psycopg
from psycopg.types.json import Jsonb

from kapy.rpc import JsonObject, RpcError

from .auth import Principal
from .storage import Metadata
from .telegram_storage import migrate

if TYPE_CHECKING:
    from .frontends import FrontendContext

logger = logging.getLogger(__name__)
COMMANDS = {
    "new": "Create a session using saved settings",
    "settings": "Show saved settings",
    "providers": "List configured providers",
    "provider": "Select a provider ID or configure one with JSON",
    "discover": "Refresh models from the selected provider",
    "models": "List saved model IDs",
    "model": "Select a model ID or JSON budget overrides",
    "modeldefaults": "Update shared model defaults with JSON",
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
    rich_content_rejected: bool = False


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


def rich_chunk(text: str) -> tuple[str, str]:
    """Conservative 32 KiB raw budget; split only on blank lines outside fences."""
    if len(text.encode("utf-8")) <= 32768:
        return text, ""
    position = size = boundary = 0
    fence = ""
    for line in text.splitlines(keepends=True):
        size += len(line.encode("utf-8"))
        if size > 32768:
            break
        position += len(line)
        content = line.rstrip("\r\n")
        marker = re.fullmatch(r" {0,3}(`{3,}|~{3,})(.*)", content)
        if marker:
            run, tail = marker.groups()
            if fence:
                if run[0] == fence[0] and len(run) >= len(fence) and not tail.strip():
                    fence = ""
            elif run[0] == "~" or "`" not in tail:
                fence = run
        elif not fence and not content.strip():
            boundary = position
    return text[:boundary], text[boundary:]


def rich_rejection(description: Any) -> bool:
    """Recognize explicit content failures only; never retain the API description."""
    if not isinstance(description, str):
        return False
    description = description.lower().removeprefix("bad request: ")
    # Architect's rejected-request samples: .context/delivery.md, commit ec34cc0.
    return description in {
        "rich_message_text_too_long",
        "rich_message_blocks_too_many",
        "rich_message_table_cols_too_many",
        "rich_message_depth_invalid",
    }


def empty_projection() -> dict[str, Any]:
    """Only install over a legacy row after an offline, verified drain."""
    return {"version": 2}


class ProjectionMigrationRequired(ValueError):
    """An operator must drain the old control before converting its projection."""


def project(records: list[dict[str, Any]], previous: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if previous and previous.get("version") != 2:
        raise ProjectionMigrationRequired(
            "Telegram projection requires offline drain and migration"
        )
    projection = copy.deepcopy(previous or empty_projection())
    for record in records:
        kind = record["kind"]
        data = record.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if record.get("cursor") is not None:
            projection["cursor"] = record["cursor"]
        if kind not in {
            "text_delta",
            "model_response",
            "tool_call",
            "tool_result",
            "final",
            "error",
            "interrupted",
            "notice",
        }:
            continue
        if kind == "notice" and data.get("kind") not in {"attempt_failed", "thinking_delta"}:
            continue
        run = record.get("run_id")
        if run is not None:
            if run != projection.get("run_id"):
                for field in ("message", "progress", "thinking", "last_response"):
                    projection.pop(field, None)
            projection["run_id"] = run
        key = record.get("message_id") or ""
        if kind == "notice" and data.get("kind") == "thinking_delta":
            text = data.get("text", "")
            if not isinstance(text, str) or not text or projection.get("message") is not None:
                continue
            marker = {"message_id": key, "part_index": data.get("part_index", 0)}
            prior = projection.get("progress", "") if projection.get("thinking") == marker else ""
            projection["progress"] = (prior + text)[-2000:]
            projection["thinking"] = marker
        elif kind == "text_delta":
            text = data.get("text", "")
            if not text:
                continue
            message: dict[str, Any] | None = projection.get("message")
            if message is None or message["id"] != key:
                message = dict[str, Any](id=key, parts={})
                projection["message"] = message
            projection.pop("progress", None)
            projection.pop("thinking", None)
            part = str(data.get("part_index", 0))
            message["parts"][part] = message["parts"].get(part, "") + text
        elif kind in {"tool_call", "tool_result", "notice", "interrupted"}:
            if kind == "notice":
                message = projection.get("message")
                active_id = (
                    message["id"]
                    if message is not None
                    else projection.get("thinking", {}).get("message_id")
                )
                if active_id is not None and active_id != data.get("failed_message_id"):
                    continue
            projection.pop("message", None)
            projection.pop("thinking", None)
            projection["progress"] = progress_text(kind, data)
        elif kind in {"model_response", "final", "error"}:
            following = empty_projection()
            if "chat_type" in projection:
                following["chat_type"] = projection["chat_type"]
            if kind == "model_response":
                text = record.get("text", "")
                projection.pop("message", None)
                projection.pop("progress", None)
                projection.pop("thinking", None)
                if not text:
                    continue
                if "run_id" in projection:
                    following["run_id"] = projection["run_id"]
                following["last_response"] = {
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                }
            elif kind == "final":
                output = data.get("output", record.get("text", ""))
                text = output if isinstance(output, str) else ""
                if hashlib.sha256(text.encode()).hexdigest() == projection.get(
                    "last_response", {}
                ).get("sha256"):
                    text = ""
            else:
                category = data.get("kind", "")
                # Unknown failures expose only their exception category; trusted
                # RunFailure records carry a separately bounded public explanation.
                safe = (
                    category
                    if isinstance(category, str) and category.isidentifier() and len(category) <= 80
                    else ""
                )
                explanation = data.get("public_message")
                text = (
                    "Sorry, I couldn’t complete this reply. " + explanation
                    if isinstance(explanation, str) and 0 < len(explanation) <= 1024
                    else "Sorry, I couldn’t complete this reply." + (f" ({safe})" if safe else "")
                )
            projection["pending"] = {
                "text": text,
                "cursor": projection.get("cursor"),
                "next": following,
                "format": "plain" if kind == "error" else "rich",
            }
            break
    message = projection.get("message")
    preview = (
        "".join(message["parts"][part] for part in sorted(message["parts"], key=int))
        if message is not None
        else projection.get("progress", "")
    )
    return preview, projection


def progress_text(kind: str, data: dict[str, Any]) -> str:
    """Only the latest, bounded progress preview; never persist it as a chat message."""
    if kind == "notice":
        return "Retrying this reply…"
    if kind == "interrupted":
        return "Resuming this reply…"
    name = data.get("name")
    label = name if isinstance(name, str) and name else "Tool"
    label += " · calling" if kind == "tool_call" else " · returned"
    content = data.get("summary", data.get("args" if kind == "tool_call" else "result", ""))
    detail = tool_summary(content, calling=kind == "tool_call")
    return short_progress(label + (f" · {detail}" if detail else ""))


def short_progress(text: str) -> str:
    """At most three lines/300 characters; never silently truncate a preview."""
    suffix = "… [truncated]"
    lines = text.splitlines()
    clipped = "\n".join(lines[:3])
    if len(lines) > 3 or len(clipped) > 300:
        return clipped[: 300 - len(suffix)] + suffix
    return clipped


def tool_summary(value: Any, *, calling: bool = False) -> str:
    """Select only direct fields and known process output; no arbitrary JSON rendering."""
    if isinstance(value, str):
        if re.search(r"[A-Za-z0-9+/=_-]{100,}", value):
            return "Long encoded content omitted"
        if calling and ("\n" in value or "\r" in value):
            return f"{len(value.splitlines())} lines, {len(value)} characters"
        return short_progress(value)
    if isinstance(value, list):
        return f"{len(value)} items"
    if not isinstance(value, dict):
        return "No content" if value is None else str(value)
    if calling:
        details = []
        for key in ("command", "path", "query", "pattern", "patch", "script", "input", "content"):
            field = value.get(key)
            if isinstance(field, str) and field:
                if key in {"patch", "script", "input", "content"}:
                    details.append(
                        f"{key}: {len(field.splitlines())} lines, {len(field)} characters"
                    )
                else:
                    details.append(tool_summary(field, calling=True))
                if len(details) == 2:
                    break
        return " · ".join(details) if details else f"{len(value)} parameters"
    if value.get("error"):
        # Keep the error category, not an unbounded provider/transport exception.
        error = value["error"]
        return "Error · " + (short_progress(error) if isinstance(error, str) else "reported")
    process = value.get("process", value)
    details = []
    if isinstance(process, dict):
        state = process.get("state")
        if isinstance(state, str):
            details.append(state)
        code = process.get("exit_code")
        if isinstance(code, int):
            details.append(f"exit {code}")
    reason = value.get("reason")
    if isinstance(reason, str) and reason not in details:
        details.append(reason)
    output = value.get("output")
    if isinstance(output, dict):
        for stream in ("stderr", "stdout", "pty"):
            chunk = output.get(stream)
            if isinstance(chunk, dict) and isinstance(chunk.get("text"), str) and chunk["text"]:
                details.append(f"{stream}: {tool_summary(chunk['text'])}")
                if chunk.get("truncated") or chunk.get("display_truncated"):
                    details.append("Output truncated")
                break
    if details:
        return " · ".join(details)
    if isinstance(value.get("items"), list):
        return f"{len(value['items'])} items"
    return f"Result with {len(value)} fields"


class TelegramFrontend:
    def __init__(self, context: FrontendContext) -> None:
        self.settings = context.settings
        self.control = context.control
        self.metadata = Metadata(context.metadata_pool, schema=context.schema)
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
        self._draft_sent: dict[int, tuple[str, bool, float]] = {}

    def principal(self, chat: int, thread: int) -> Principal:
        return Principal(
            "frontend", frontend_id="telegram", subject=f"{self.bot_id}:{chat}:{thread}"
        )

    async def session_view(self, session_id: str, chat: int, thread: int) -> dict[str, Any] | None:
        try:
            view = cast(
                dict[str, Any],
                await self.control.call(
                    "session.get",
                    {"session_id": session_id},
                    principal=self.principal(chat, thread),
                ),
            )
            if view["status"] != "deleting":
                return view
        except RpcError as exc:
            if exc.code not in {-32004, -32001}:
                raise
        # Plugin-owned stale state is cleaned even after it was disabled during deletion.
        async with self.metadata.connection() as conn:
            await conn.execute(
                "DELETE FROM gateway_telegram_delivery WHERE bot_id=%s AND chat_id=%s "
                "AND thread_id=%s AND session_id=%s",
                (self.bot_id, chat, thread, session_id),
            )
            await conn.execute(
                "UPDATE gateway_telegram_routes SET session_id=NULL WHERE "
                "bot_id=%s AND chat_id=%s AND thread_id=%s AND session_id=%s",
                (self.bot_id, chat, thread, session_id),
            )
        return None

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
                rejected = (
                    method in {"sendRichMessage", "sendRichMessageDraft"}
                    and response.status_code == 400
                    and code == 400
                    and body.get("ok") is False
                    and rich_rejection(body.get("description"))
                )
                raise TelegramFailure(
                    code, body.get("parameters", {}).get("retry_after", 1), rejected
                )
            return body["result"]
        except httpx2.HTTPError, ValueError:
            raise TelegramFailure(503, random.uniform(1, 3)) from None

    async def send(self, chat: int, thread: int, text: str) -> None:
        await self._send("sendMessage", chat, thread, text)

    async def send_draft(self, chat: int, thread: int, draft_id: int, text: str) -> None:
        await self._send("sendMessageDraft", chat, thread, text, draft_id=draft_id)

    async def send_rich(self, chat: int, thread: int, text: str) -> None:
        await self._send("sendRichMessage", chat, thread, text)

    async def send_rich_draft(self, chat: int, thread: int, draft_id: int, text: str) -> None:
        await self._send("sendRichMessageDraft", chat, thread, text, draft_id=draft_id)

    async def _send(self, method: str, chat: int, thread: int, text: str, **extra: Any) -> None:
        lock = self._chat_locks.setdefault(chat, asyncio.Lock())
        async with lock:
            while (delay := self._chat_ready.get(chat, 0) - time.monotonic()) > 0:  # noqa: ASYNC110 - deadline
                await asyncio.sleep(delay)
            params: dict[str, Any] = {"chat_id": chat, **extra}
            if method in {"sendRichMessage", "sendRichMessageDraft"}:
                params["rich_message"] = {"markdown": text}
            else:
                params["text"] = text
            if thread:
                params["message_thread_id"] = thread
            try:
                await self.api(method, params)
            except TelegramFailure as exc:
                self._chat_ready[chat] = time.monotonic() + retry_delay(exc)
                raise
            self._chat_ready[chat] = time.monotonic() + 1

    async def run(self) -> None:
        await migrate(self.metadata.pool, schema=self.metadata.schema)
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
            except psycopg.Error, OSError, RpcError:
                logger.warning("Telegram inbox storage unavailable; polling will retry")
                await asyncio.sleep(retry_delay(TelegramFailure(503), failures))
                failures += 1

    async def route(self, chat: int, thread: int) -> dict[str, Any]:
        default: JsonObject = {
            "title": "Telegram",
            "machine_ids": [],
            "default_machine_id": None,
            "config": {},
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
        view = await self.session_view(sid, inbox["chat_id"], inbox["thread_id"]) if sid else None
        if sid and view is None:
            sid = None
        if command == "/help":
            return {
                "kind": "reply",
                "text": "\n".join(
                    f"/{name}: {description}" for name, description in COMMANDS.items()
                ),
            }
        if command == "/settings":
            return {"kind": "reply", "text": json.dumps(redact_config(config), ensure_ascii=False)}
        if command == "/status":
            return (
                {"kind": "status", "session_id": sid}
                if sid
                else {
                    "kind": "reply",
                    "text": "No active session. Use /new or send text.",
                }
            )
        if command in {"/providers", "/provider", "/discover", "/models", "/modeldefaults"}:
            params: dict[str, Any] = {}
            selected_provider = config.get("provider_id")
            select = False
            if command == "/providers":
                method = "provider.list"
            elif command == "/provider":
                select = True
                if argument.lstrip().startswith("{"):
                    try:
                        params = json.loads(argument)
                        if not isinstance(params, dict):
                            raise ValueError
                    except ValueError:
                        return {
                            "kind": "reply",
                            "text": "Provider configuration must be a JSON object.",
                        }
                    method = "provider.update" if "provider_id" in params else "provider.create"
                else:
                    method, params = "provider.get", {"provider_id": argument.strip()}
            elif command == "/modeldefaults":
                selected_model = config.get("config", {}).get("model", {}).get("model_id")
                if selected_model is None:
                    return {"kind": "reply", "text": "Choose /model ID first."}
                try:
                    defaults = json.loads(argument)
                    if not isinstance(defaults, dict):
                        raise ValueError
                except ValueError:
                    return {"kind": "reply", "text": "Defaults must be a JSON object."}
                current = cast(
                    dict,
                    await self.control.call(
                        "provider.model.get",
                        {"model_id": selected_model},
                        principal=self.principal(inbox["chat_id"], inbox["thread_id"]),
                    ),
                )
                method, params = (
                    "provider.model.update",
                    {
                        "model_id": selected_model,
                        "expected_revision": current["revision"],
                        "defaults": defaults,
                    },
                )
            else:
                if selected_provider is None:
                    return {"kind": "reply", "text": "Choose /provider ID first."}
                method = "provider.discover" if command == "/discover" else "provider.models"
                params = {"provider_id": selected_provider}
                if command == "/discover" and argument.strip():
                    params["page_token"] = argument.strip()
            if method in {
                "provider.create",
                "provider.update",
                "provider.discover",
                "provider.model.update",
            }:
                params["request_id"] = request_id(self.bot_id, inbox["update_id"], method)
            return {
                "kind": "provider",
                "method": method,
                "params": params,
                "select": select,
                "config": config,
            }
        if command in {"/model", "/machine", "/instructions"}:
            if not argument.strip():
                return {"kind": "reply", "text": f"Usage: {command} <value>"}
            if command == "/machine":
                if argument not in self.settings.machine_tokens:
                    return {"kind": "reply", "text": "Unknown configured machine."}
                config["machine_ids"] = [argument]
                config["default_machine_id"] = argument
            elif command == "/model":
                try:
                    selected = (
                        json.loads(argument)
                        if argument.lstrip().startswith("{")
                        else {"model_id": argument.strip()}
                    )
                    from .models import parse_session_model

                    selected = parse_session_model(selected).model_dump(mode="json")
                    registered = cast(
                        dict,
                        await self.control.call(
                            "provider.model.get",
                            {"model_id": selected["model_id"]},
                            principal=self.principal(inbox["chat_id"], inbox["thread_id"]),
                        ),
                    )
                    config["provider_id"] = registered["provider_id"]
                    config["config"]["model"] = selected
                except ValueError, TypeError:
                    return {
                        "kind": "reply",
                        "text": "Use /model ID or a model selection JSON object.",
                    }
            else:
                config["config"][command[1:]] = argument
            update = False
            if sid and command != "/instructions":
                update = view is not None and view["status"] == "waiting"
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
            if not isinstance(config["config"].get("model"), dict):
                return {
                    "kind": "reply",
                    "text": "Choose a configured model with /provider and /model first.",
                }
            if not config["machine_ids"]:
                return {"kind": "reply", "text": "Choose a machine with /machine <id> first."}
            return {
                "kind": "create",
                "params": {
                    **session_configuration(config),
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
        principal = self.principal(chat, thread)
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
                action["session_id"] = sid
                async with self.metadata.connection() as conn:
                    await conn.execute(
                        "UPDATE gateway_telegram_inbox SET resolved_action=%s "
                        "WHERE bot_id=%s AND update_id=%s",
                        (Jsonb(action), self.bot_id, inbox["update_id"]),
                    )
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
        elif kind == "provider":
            result = cast(
                dict,
                await self.control.call(action["method"], action["params"], principal=principal),
            )
            config = action["config"]
            if action["select"]:
                config["provider_id"] = result["id"]
                config["config"].pop("model", None)
            if action["select"] or action["method"] == "provider.discover":
                listing = cast(
                    dict,
                    await self.control.call(
                        "provider.models",
                        {"provider_id": config["provider_id"]},
                        principal=principal,
                    ),
                )
                if listing["default_model_id"] is not None and not config["config"].get("model"):
                    config["config"]["model"] = {"model_id": listing["default_model_id"]}
                await self.metadata.rows(
                    "UPDATE gateway_telegram_routes SET config=%s "
                    "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s",
                    (Jsonb(config), self.bot_id, chat, thread),
                )
            reply = json.dumps(result, ensure_ascii=False)
            if action["method"] in {"provider.update", "provider.model.update"}:
                reply += "\nSaved. This shared setting applies to future runs of sessions using it."
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
                            **session_configuration(action["config"]),
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
            # Provider configuration is private while pending; discard key-bearing ingress
            # once its durable operation and user acknowledgement have completed.
            if message_command(inbox.get("payload", {})) == "/provider":
                await conn.execute(
                    "UPDATE gateway_telegram_inbox SET payload='{}',resolved_action=%s "
                    "WHERE bot_id=%s AND update_id=%s",
                    (
                        Jsonb({"kind": "reply", "text": reply or "Settings saved."}),
                        self.bot_id,
                        inbox["update_id"],
                    ),
                )
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
                    private_config = message_command(inbox.get("payload", {})) == "/provider"
                    await self.metadata.rows(
                        "UPDATE gateway_telegram_inbox SET handled=true,"
                        "payload=CASE WHEN %s THEN '{}'::jsonb ELSE payload END,"
                        "resolved_action=CASE WHEN %s THEN '{}'::jsonb ELSE resolved_action END "
                        "WHERE bot_id=%s AND update_id=%s",
                        (private_config, private_config, self.bot_id, inbox["update_id"]),
                    )
                else:
                    await self.metadata.rows(
                        "UPDATE gateway_telegram_inbox "
                        "SET next_attempt_at=now()+%s*interval '1 second' "
                        "WHERE bot_id=%s AND update_id=%s",
                        (retry_delay(exc), self.bot_id, inbox["update_id"]),
                    )
            except RpcError as exc:
                discovery_failed = (
                    message_command(inbox.get("payload", {})) == "/discover" and exc.code == -32030
                )
                if discovery_failed or exc.code in {-32602, -32001, -32004, -32009, -32020}:
                    # Explicit discovery is a user command: report failure without
                    # retrying its network request ahead of repair commands in this topic.
                    # The saved reply still uses normal Telegram send/ACK retries.
                    await self.metadata.rows(
                        "UPDATE gateway_telegram_inbox SET resolved_action=%s "
                        "WHERE bot_id=%s AND update_id=%s",
                        (
                            Jsonb(
                                {
                                    "kind": "reply",
                                    "text": (
                                        "Model discovery failed. Check provider settings "
                                        "and use /discover to retry."
                                        if discovery_failed
                                        else exc.message
                                    ),
                                }
                            ),
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
            except psycopg.Error, OSError, RpcError:
                logger.warning("Telegram processing storage unavailable; processing will retry")
                await asyncio.sleep(retry_delay(TelegramFailure(503), failures))
                failures += 1
            await asyncio.sleep(0.25)

    async def restore_origins(self) -> None:
        """Resolve only retained legacy deliveries, using their exact saved create receipt."""
        rows = await self.metadata.rows(
            "SELECT i.* FROM gateway_telegram_inbox i WHERE i.bot_id=%s AND i.handled "
            "AND i.resolved_action->>'kind'='create' AND NOT i.resolved_action ? 'session_id' "
            "AND EXISTS(SELECT 1 FROM gateway_telegram_delivery d WHERE d.bot_id=i.bot_id "
            "AND d.chat_id=i.chat_id AND d.thread_id=i.thread_id)",
            (self.bot_id,),
        )
        for row in rows:
            action = row["resolved_action"]
            result = cast(
                dict[str, Any],
                await self.control.call(
                    "session.create",
                    action["params"],
                    principal=self.principal(row["chat_id"], row["thread_id"]),
                ),
            )
            action["session_id"] = result["session"]["id"]
            await self.metadata.rows(
                "UPDATE gateway_telegram_inbox SET resolved_action=%s WHERE "
                "bot_id=%s AND update_id=%s",
                (Jsonb(action), self.bot_id, row["update_id"]),
            )

    async def deliver_once(self) -> None:
        await self.restore_origins()
        rows = await self.metadata.rows(
            "SELECT d.*, origin.update_id FROM gateway_telegram_delivery d "
            "LEFT JOIN LATERAL (SELECT min(i.update_id) AS update_id "
            "FROM gateway_telegram_inbox i "
            "WHERE i.bot_id=d.bot_id AND i.resolved_action->>'session_id'=d.session_id::text "
            "AND i.resolved_action->>'kind'='create') origin ON true "
            "WHERE d.bot_id=%s ORDER BY d.chat_id,d.thread_id,"
            "origin.update_id NULLS FIRST,d.session_id",
            (self.bot_id,),
        )
        busy: set[tuple[int, int]] = set()
        for row in rows:
            if (
                await self.session_view(str(row["session_id"]), row["chat_id"], row["thread_id"])
                is None
            ):
                continue
            route = (row["chat_id"], row["thread_id"])
            if route in busy:
                continue
            if row["update_id"] is None:
                # An old create may still be retrying its confirmation reply. Its
                # original inbox action will restore the origin; hold this route
                # until then instead of guessing the unknown delivery's order.
                busy.add(route)
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
        key = (self.bot_id, row["chat_id"], row["thread_id"], row["session_id"])
        projection = row["projection"]
        if projection and projection.get("version") != 2:
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
                page = cast(
                    dict[str, Any],
                    await self.control.call(
                        "session.output",
                        {
                            "session_id": str(row["session_id"]),
                            "after": cursor,
                            "limit": 200,
                            "wait_seconds": 0,
                        },
                        principal=self.principal(row["chat_id"], row["thread_id"]),
                    ),
                )
                preview, projection = project(page["items"], projection)
                cursor = projection.pop("cursor", page["next_cursor"])
                pending = projection.get("pending")
                has_more = page["has_more"] or cursor != page["next_cursor"]
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
                remaining = pending["text"][offset:]
                rich = pending.get("format") == "rich"
                chunk, remainder = rich_chunk(remaining) if rich else text_chunk(remaining)
                if rich and not chunk and remaining:
                    pending["format"] = "plain"
                    await self._save_projection(projection, key)
                    rich = False
                    chunk, remainder = text_chunk(remaining)
                if chunk:
                    if (
                        await self.session_view(
                            str(row["session_id"]), row["chat_id"], row["thread_id"]
                        )
                        is None
                    ):
                        return False
                    try:
                        if rich:
                            await self.send_rich(row["chat_id"], row["thread_id"], chunk)
                        else:
                            await self.send(row["chat_id"], row["thread_id"], chunk)
                    except TelegramFailure as exc:
                        if not rich or not exc.rich_content_rejected:
                            raise
                        pending["format"] = "plain"
                        await self._save_projection(projection, key)
                        return True
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
                    message = projection.get("message")
                    rich = message is not None and not message.get("plain")
                    text = rich_chunk(preview)[0] if rich else text_chunk(preview)[0]
                    if rich and not text and preview:
                        assert message is not None
                        message["plain"] = True
                        await self._save_projection(projection, key)
                        rich = False
                        text = text_chunk(preview)[0]
                    sent = self._draft_sent.get(draft["id"])
                    if sent is None or sent[:2] != (text, rich) or time.monotonic() - sent[2] >= 20:
                        try:
                            send = self.send_rich_draft if rich else self.send_draft
                            await send(row["chat_id"], row["thread_id"], draft["id"], text)
                            self._draft_sent[draft["id"]] = (text, rich, time.monotonic())
                        except TelegramFailure as exc:
                            if rich and exc.rich_content_rejected:
                                assert message is not None
                                message["plain"] = True
                            elif not rich and exc.code == 400:
                                draft["unavailable"] = True
                            else:
                                raise
                await self.metadata.rows(
                    "UPDATE gateway_telegram_delivery SET projection=%s WHERE bot_id=%s "
                    "AND chat_id=%s AND thread_id=%s AND session_id=%s",
                    (Jsonb(projection), *key),
                )
            view = await self.session_view(str(row["session_id"]), row["chat_id"], row["thread_id"])
            return view is not None and (
                has_more or bool(projection.get("run_id")) or view["status"] != "waiting"
            )
        except RpcError as exc:
            if exc.code not in {-32004, -32001}:
                raise
            await self.session_view(str(row["session_id"]), row["chat_id"], row["thread_id"])
            return False
        except TelegramFailure as exc:
            await self.metadata.rows(
                "UPDATE gateway_telegram_delivery SET blocked_error=%s, "
                "next_attempt_at=now()+%s*interval '1 second' "
                "WHERE bot_id=%s AND chat_id=%s AND thread_id=%s AND session_id=%s",
                (str(exc.code) if exc.code in {400, 403} else None, retry_delay(exc), *key),
            )
            return True

    async def _save_projection(self, projection: dict[str, Any], key: tuple[Any, ...]) -> None:
        await self.metadata.rows(
            "UPDATE gateway_telegram_delivery SET projection=%s WHERE bot_id=%s "
            "AND chat_id=%s AND thread_id=%s AND session_id=%s",
            (Jsonb(projection), *key),
        )

    async def deliver(self) -> None:
        failures = 0
        while not self.disabled:
            try:
                await self.deliver_once()
                failures = 0
            except psycopg.Error, OSError, RpcError:
                logger.warning("Telegram delivery storage unavailable; delivery will retry")
                await asyncio.sleep(retry_delay(TelegramFailure(503), failures))
                failures += 1
            await asyncio.sleep(1)


def session_configuration(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config[key] for key in ("title", "machine_ids", "default_machine_id", "config")}


def redact_config(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("[configured]" if key == "api_key" else redact_config(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_config(item) for item in value]
    return value


def message_command(payload: dict) -> str:
    text = payload.get("message", {}).get("text", "")
    return text.partition(" ")[0].split("@")[0]

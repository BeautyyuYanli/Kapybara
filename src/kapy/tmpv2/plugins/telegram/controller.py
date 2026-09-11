"""Session-only Telegram input adapter with durable, at-least-once business calls.

The inbox consumer is serial. Resolved targets and templates survive retries;
core commits and private SQLite progress deliberately remain separate transactions.
Polling and recovery never hold a transaction during a network or runner call.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from kapy.tmpv2.control.sessions import CreateSession, SessionService, SubmitInput

from .client import TelegramClient, TelegramFailure, retry_delay
from .models import InboxRow
from .repository import TelegramRepository
from .settings import TelegramSettings

logger = logging.getLogger(__name__)
COMMANDS = {
    "new": "Create a new session",
    "queue": "Queue input",
    "steer": "Steer the current run",
    "status": "Show session status",
    "cancel": "Request cancellation",
    "help": "Show commands",
}
HELP = "\n".join(f"/{command} — {description}" for command, description in COMMANDS.items())


class TelegramController:
    def __init__(
        self,
        *,
        client: TelegramClient,
        sessions: SessionService,
        repository: TelegramRepository,
        settings: TelegramSettings,
        bot_id: int,
        username: str,
        schedule_runner: Callable[[UUID], None],
    ) -> None:
        self.client, self.sessions, self.repository = client, sessions, repository
        self.settings, self.bot_id, self.username = settings, bot_id, username
        self.schedule_runner = schedule_runner

    async def poll(self) -> None:
        failures = 0
        while True:
            try:
                updates = await self.client.api(
                    "getUpdates",
                    {
                        "offset": await self.repository.offset(self.bot_id),
                        "timeout": self.settings.poll_timeout,
                        "allowed_updates": ["message"],
                    },
                )
                await self.repository.ingest(self.bot_id, updates)
                failures = 0
            except TelegramFailure as error:
                if error.code in {400, 401, 403, 409}:
                    raise
                await asyncio.sleep(retry_delay(error, failures))
                failures += 1
            except SQLAlchemyError:
                logger.warning("Telegram polling storage unavailable")
                await asyncio.sleep(retry_delay(TelegramFailure(503), failures))
                failures += 1

    async def resolve(self, item: InboxRow) -> dict[str, Any]:
        message = item.payload.get("message") or {}
        sender = message.get("from") or {}
        if item.chat_id not in self.settings.allowed_chat_ids or not sender or sender.get("is_bot"):
            return {"command": "ignore"}
        if not isinstance(message.get("text"), str):
            return {"command": "reply", "reply": "Only text messages are supported."}
        text = message["text"]
        command = "queue"
        explicit = text.startswith("/")
        if explicit:
            words = text.split(maxsplit=1)
            token, _, target = words[0][1:].partition("@")
            if target and target.lower() != self.username.lower():
                return {"command": "ignore"}
            command, text = token.lower(), words[1] if len(words) == 2 else ""
            if command not in COMMANDS:
                return {"command": "reply", "reply": "Unknown command. Use /help."}
        if command == "help":
            return {"command": "reply", "reply": HELP}
        if command in {"queue", "steer"} and not text.strip():
            return {"command": "reply", "reply": f"Usage: /{command} <text>"}
        session_id = await self.repository.route(self.bot_id, item.chat_id, item.thread_id)
        if session_id is not None:
            try:
                await self.sessions.get_session(session_id)
            except LookupError:
                await self.repository.clear_route(
                    self.bot_id, item.chat_id, item.thread_id, session_id
                )
                session_id = None
        if command in {"status", "cancel"} and session_id is None:
            return {"command": "reply", "reply": "No current session. Use /new or send text."}
        create = command == "new" or session_id is None
        return {
            "command": command,
            "text": text,
            "session_id": None if create else str(session_id),
            "template": self.settings.session_template.model_dump(mode="json") if create else None,
        }

    async def handle(self, item: InboxRow) -> None:
        action = item.resolved_action
        if action is None:
            action = await self.resolve(item)
            await self.repository.save_action(item, action)
        action = dict(action)
        command = action["command"]
        if command not in {"ignore", "reply"} and not action.get("completed"):
            if action["session_id"] is None:
                session = await self.sessions.create_session(
                    CreateSession.model_validate(action["template"])
                )
                await self.repository.bind(item, action, session.id)
                action = dict(item.resolved_action or {})
            session_id = UUID(action["session_id"])
            if command in {"queue", "steer", "new"}:
                if action["text"] and not action.get("submitted"):
                    submission = await self.sessions.submit_input(
                        session_id,
                        SubmitInput(
                            content=action["text"],
                            channel="steer" if command == "steer" else "queued",
                        ),
                    )
                    action["submitted"] = True
                    await self.repository.save_action(item, action)
                    if submission.should_start_runner:
                        self.schedule_runner(session_id)
                if command == "new":
                    action["reply"] = "New session created."
            elif command == "cancel":
                await self.sessions.request_cancel(session_id)
                action["reply"] = "Cancellation requested."
            elif command == "status":
                running = await self.sessions.is_runner_running(session_id)
                cancelled = await self.sessions.read_cancel(session_id)
                queued = await self.sessions.read_inputs(session_id, "queued")
                steer = await self.sessions.read_inputs(session_id, "steer")
                action["reply"] = (
                    f"Session: {session_id}\nLease: {'running' if running else 'idle'}\n"
                    f"Cancellation requested: {cancelled}\n"
                    f"Queued: {len(queued)}; steer: {len(steer)}"
                )
            action["completed"] = True
            await self.repository.save_action(item, action)
        if reply := action.get("reply"):
            await self.client.send(item.chat_id, item.thread_id, reply)
        await self.repository.save_action(item, action, handled=True)

    async def process_once(self) -> bool:
        item = await self.repository.next_inbox(self.bot_id)
        if item is None or item.next_attempt_at > time.time():
            return False
        try:
            await self.handle(item)
        except TelegramFailure as error:
            if error.code == 401:
                raise
            await self.repository.save_action(
                item,
                item.resolved_action or {},
                handled=error.code in {400, 403},
                next_attempt_at=time.time() + retry_delay(error),
            )
        except (LookupError, ValueError) as error:
            # Definite business validation failures are safe to report by category only.
            await self.repository.save_action(
                item,
                {
                    "command": "reply",
                    "reply": f"Session operation failed ({type(error).__name__}).",
                },
            )
        return True

    async def recover(self) -> None:
        """Schedule only known Telegram sessions with pending input and no valid lease."""
        for session_id in {row.session_id for row in await self.repository.deliveries(self.bot_id)}:
            if not await self.sessions.is_runner_running(session_id) and (
                await self.sessions.read_inputs(session_id, "queued")
                or await self.sessions.read_inputs(session_id, "steer")
            ):
                self.schedule_runner(session_id)

    async def process(self) -> None:
        next_recovery = 0.0
        while True:
            try:
                if time.monotonic() >= next_recovery:
                    await self.recover()
                    next_recovery = time.monotonic() + self.settings.recovery_interval
                if not await self.process_once():
                    await asyncio.sleep(0.25)
            except SQLAlchemyError:
                logger.warning("Telegram input storage unavailable")
                await asyncio.sleep(1)

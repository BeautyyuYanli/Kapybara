"""Session-only Telegram input adapter with durable, at-least-once business calls.

The inbox consumer is serial. Resolved targets and templates survive retries;
core commits and private SQLite progress deliberately remain separate transactions.
Polling and recovery never hold a transaction during a network or runner call.
The model catalog is read only: /model changes the bound session's model pair and
persists the bot's default in private storage. Session progress is saved before
changing the default; retries keep their resolved target. Running agents retain
their existing model snapshot until the next run.
"""

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from kapy.control.models import ModelService
from kapy.control.sessions import CreateSession, SessionService, SubmitInput, UpdateSession

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
    "model": "Set the session model and bot default, or show the default",
    "help": "Show commands",
}
HELP = "\n".join(f"/{command} — {description}" for command, description in COMMANDS.items())
MODEL_USAGE = "Usage: /model <provider UUID> <model name>"


class TelegramController:
    def __init__(
        self,
        *,
        client: TelegramClient,
        sessions: SessionService,
        models: ModelService,
        repository: TelegramRepository,
        settings: TelegramSettings,
        bot_id: int,
        username: str,
        schedule_runner: Callable[[UUID], None],
    ) -> None:
        self.client, self.sessions, self.repository = client, sessions, repository
        self.models = models
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

    async def session_template(self) -> CreateSession | None:
        """Saved selection overrides the environment template using normal session defaults."""
        default = await self.repository.default_model(self.bot_id)
        if default is not None:
            return CreateSession(provider_id=default.provider_id, model_name=default.model_name)
        return self.settings.session_template

    async def resolve_model(self, text: str, session_id: UUID | None) -> dict[str, Any]:
        """Validate a complete model identity before recording a repeatable selection action."""
        if not text.strip():
            template = await self.session_template()
            current = (
                f"Default provider: {template.provider_id}\nDefault model: {template.model_name}"
                if template is not None
                else "No default session model is configured."
            )
            return {"command": "reply", "reply": f"{current}\n{MODEL_USAGE}"}
        parts = text.split(maxsplit=1)
        try:
            if len(parts) != 2:
                raise ValueError("A provider and model are required")
            choice = CreateSession(provider_id=UUID(parts[0]), model_name=parts[1])
        except ValueError:
            return {"command": "reply", "reply": MODEL_USAGE}
        try:
            await self.models.get_model(choice.provider_id, choice.model_name)
        except LookupError:
            return {
                "command": "reply",
                "reply": "That provider/model is not configured. Choose an existing model.",
            }
        return {
            "command": "model",
            "session_id": str(session_id) if session_id is not None else None,
            "provider_id": str(choice.provider_id),
            "model_name": choice.model_name,
            "reply": (
                f"Default provider: {choice.provider_id}\nDefault model: {choice.model_name}\n"
                + (
                    "Current session updated; the new model applies on its next run."
                    if session_id is not None
                    else "Saved for this bot's new sessions. Use /new to start one."
                )
            ),
        }

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
        if command == "model":
            return await self.resolve_model(text, session_id)
        if command in {"status", "cancel"} and session_id is None:
            return {"command": "reply", "reply": "No current session. Use /new or send text."}
        create = command == "new" or session_id is None
        template = await self.session_template() if create else None
        if create and template is None:
            return {
                "command": "reply",
                "reply": f"No default session model is configured.\n{MODEL_USAGE}",
            }
        return {
            "command": command,
            "text": text,
            "session_id": None if create else str(session_id),
            "template": template.model_dump(mode="json")
            if create and template is not None
            else None,
        }

    async def handle(self, item: InboxRow) -> None:
        action = item.resolved_action
        if action is None:
            action = await self.resolve(item)
            await self.repository.save_action(item, action)
        action = dict(action)
        command = action["command"]
        if command == "model" and not action.get("completed"):
            if action["session_id"] is not None and not action.get("session_updated"):
                await self.sessions.update_session(
                    UUID(action["session_id"]),
                    UpdateSession(
                        provider_id=UUID(action["provider_id"]), model_name=action["model_name"]
                    ),
                )
                action["session_updated"] = True
                await self.repository.save_action(item, action)
            await self.repository.set_default_model(
                self.bot_id, UUID(action["provider_id"]), action["model_name"]
            )
            action["completed"] = True
            await self.repository.save_action(item, action)
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
                    f"Session: {session_id}\nLease: {'busy' if running else 'idle'}\n"
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

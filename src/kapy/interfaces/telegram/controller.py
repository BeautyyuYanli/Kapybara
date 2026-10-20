"""Session-only Telegram input adapter with durable, at-least-once business calls.

The inbox consumer is serial. Fixed targets/templates, preallocated session IDs and
create/run/close Activity UUIDs survive retries. Recovery queries the attempted
UUID without resubmission. Missing native records finish with an unconfirmed
reply so one old message cannot block all later inbox work. Core and private SQLite progress
deliberately remain separate transactions. Explicit close targets never rebind the route.
Polling and recovery never hold a transaction during a network or runner call.
The model catalog is read only: /model changes the bound session's model pair and
persists the bot's default in private storage. Session progress is saved before
changing the default; retries keep their resolved target. Running agents retain
their existing model snapshot until the next run.
"""

import asyncio
import logging
import time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.exc import SQLAlchemyError
from temporalio.service import RPCError, RPCStatusCode

from kapy.control.models import ModelService
from kapy.control.sessions import (
    ActivityIdentityConflict,
    CreateSession,
    SessionExecutionError,
    SessionService,
    UpdateSession,
)
from kapy.control.sessions.temporal import CLOSE, CREATE
from kapy.lifecycle import LifecycleError

from .client import TelegramClient, TelegramFailure, retry_delay
from .models import InboxRow
from .repository import TelegramRepository
from .settings import TelegramSettings

logger = logging.getLogger(__name__)
COMMANDS = {
    "new": "Create a new session",
    "status": "Show session status",
    "close": "Close this session, or /close <session UUID> to release that session's resources",
    "model": "Set the session model and bot default, or show the default",
    "help": "Show commands",
}
HELP = "\n".join(f"/{command} — {description}" for command, description in COMMANDS.items())
MODEL_USAGE = "Usage: /model <provider UUID> <model name>"
CLOSE_USAGE = "Usage: /close [session UUID]"


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
    ) -> None:
        self.client, self.sessions, self.repository = client, sessions, repository
        self.models = models
        self.settings, self.bot_id, self.username = settings, bot_id, username

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
        """Saved selection replaces the model pair, preserving configured session options."""
        default = await self.repository.default_model(self.bot_id)
        if default is None:
            return self.settings.session_template
        if self.settings.session_template is None:
            return CreateSession(provider_id=default.provider_id, model_name=default.model_name)
        return self.settings.session_template.model_copy(
            update={"provider_id": default.provider_id, "model_name": default.model_name}
        )

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
        command = "run"
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
        if command == "close" and text.strip():
            try:
                target = UUID(text.strip())
            except ValueError:
                return {"command": "reply", "reply": CLOSE_USAGE}
            # An abandoned creation is not bound. Explicit recovery must never
            # close or replace the chat's previous binding, even if it is stale.
            return {"command": "close", "text": "", "session_id": str(target), "template": None}
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
        if command == "status":
            return {"command": "status", "session_id": str(session_id) if session_id else None}
        if command == "close" and session_id is None:
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

    async def status(self, item: InboxRow, session_id: UUID | None) -> str:
        lines = []
        references: list[tuple[UUID, UUID, str | None]] = []
        if session_id is not None:
            session = await self.sessions.get_session(session_id)
            lines.extend([f"Session: {session_id}", f"Status: {session.status}"])
            if session.current_activity_id is not None:
                references.append(
                    (session_id, session.current_activity_id, session.current_activity_run_id)
                )
        previous = await self.repository.latest_request(item)
        action = dict(previous.resolved_action or {}) if previous else {}
        latest = None
        if action:
            target = action.get("session_id") or action.get("creating_session_id")
            for kind in ("close", "run", "create"):
                if target and action.get(kind + "_activity_id"):
                    reference = (
                        UUID(target),
                        UUID(action[kind + "_activity_id"]),
                        action.get(kind + "_run_id"),
                    )
                    if reference not in references:
                        references.append(reference)
                    latest = (UUID(target), UUID(action[kind + "_activity_id"]), kind)
                    break
        for target, activity_id, run_id in references:
            try:
                execution = await self.sessions.get_activity(target, activity_id, run_id=run_id)
            except LookupError:
                lines.append(
                    f"Session: {target}\nActivity: {activity_id}\nOutcome cannot be confirmed."
                )
            else:
                if (
                    previous is not None
                    and latest is not None
                    and latest[:2] == (target, activity_id)
                ):
                    key = latest[2] + "_run_id"
                    if action.get(key) != execution.run_id:
                        action[key] = execution.run_id
                        await self.repository.save_action(
                            previous,
                            action,
                            handled=previous.handled,
                            next_attempt_at=previous.next_attempt_at,
                        )
                lines.append(
                    f"Session: {target}\nActivity: {execution.activity_id}\n"
                    f"Run: {execution.run_id}\n"
                    f"Temporal: {execution.status}"
                    + (f" / {execution.run_state}" if execution.run_state else "")
                    + (f"\nFailure: {execution.error}" if execution.error else "")
                )
        return "\n".join(lines) or "No current session or saved request. Use /new or send text."

    async def remember_reference(
        self,
        item: InboxRow,
        action: dict[str, Any],
        kind: str,
        session_id: UUID,
    ) -> None:
        """Keep an observed native reference without undoing a confirmed business result."""
        try:
            execution = await self.sessions.get_activity(
                session_id,
                UUID(action[kind + "_activity_id"]),
                run_id=action.get(kind + "_run_id"),
            )
        except LookupError, RPCError, ActivityIdentityConflict:
            # The result was already received. Retain its UUID when a follow-up
            # reference lookup is unavailable; later /status may resolve it.
            return
        if execution.activity_type != {"create": CREATE, "close": CLOSE}[kind]:
            return
        action[kind + "_run_id"] = execution.run_id
        await self.repository.save_action(item, action)

    async def handle(self, item: InboxRow) -> None:
        resolved = item.resolved_action
        if resolved is None:
            resolved = await self.resolve(item)
            await self.repository.save_action(item, resolved)
        action: dict[str, Any] = dict(resolved)
        command = action["command"]
        if command not in {*COMMANDS, "run", "ignore", "reply"}:
            # Persisted inbox actions may predate removal of legacy commands. They
            # must not create a session or pretend to submit obsolete operations.
            action = {"command": "reply", "reply": "Unknown command. Use /help."}
            command = "reply"
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
        if command == "status" and not action.get("completed"):
            action["reply"] = await self.status(
                item,
                UUID(action["session_id"]) if action["session_id"] else None,
            )
            action["completed"] = True
            await self.repository.save_action(item, action)
        if command not in {"ignore", "reply"} and not action.get("completed"):
            if action["session_id"] is None:
                # Save the identity before the cross-database call. A core COMMIT
                # can succeed without SQLite observing its response.
                if not action.get("creating_session_id"):
                    action["creating_session_id"] = str(uuid4())
                    await self.repository.save_action(item, action)
                candidate = UUID(action["creating_session_id"])
                if not action.get("create_activity_id"):
                    action["create_activity_id"] = str(uuid4())
                    await self.repository.save_action(item, action)
                    session = await self.sessions.create_session(
                        CreateSession.model_validate(action["template"]).model_copy(
                            update={"id": candidate}
                        ),
                        activity_id=UUID(action["create_activity_id"]),
                    )
                    await self.remember_reference(item, action, "create", candidate)
                else:
                    execution = await self.sessions.get_activity(
                        candidate,
                        UUID(action["create_activity_id"]),
                        run_id=action.get("create_run_id"),
                    )
                    action["create_run_id"] = execution.run_id
                    await self.repository.save_action(item, action)
                    session = await self.sessions.wait_session_activity(
                        candidate,
                        execution.activity_id,
                        run_id=execution.run_id,
                        activity_type=CREATE,
                    )
                await self.repository.bind(item, action, session.id)
                action = dict(item.resolved_action or {})
            session_id = UUID(action["session_id"])
            if command in {"run", "new"}:
                if action["text"] and not action.get("submitted"):
                    try:
                        if not action.get("run_activity_id"):
                            action["run_activity_id"] = str(uuid4())
                            await self.repository.save_action(item, action)
                            handle = await self.sessions.start_runner(
                                session_id,
                                user_prompt=action["text"],
                                activity_id=UUID(action["run_activity_id"]),
                            )
                            action["run_run_id"] = handle.run_id
                        else:
                            execution = await self.sessions.get_activity(
                                session_id,
                                UUID(action["run_activity_id"]),
                                run_id=action.get("run_run_id"),
                            )
                            if execution.activity_type != "kapy.run_agent":
                                raise ActivityIdentityConflict(
                                    session_id, execution.activity_id, execution.run_id
                                )
                            action["run_run_id"] = execution.run_id
                    except ActivityIdentityConflict:
                        action["reply"] = (
                            "Previous execution is unsuccessful or unconfirmed. "
                            "Use /new to continue."
                        )
                    else:
                        action["submitted"] = True
                        action["reply"] = (
                            f"Submitted.\nSession: {session_id}\n"
                            f"Activity: {action['run_activity_id']}\n"
                            f"Run: {action['run_run_id']}\nUse /status to check execution."
                        )
                    await self.repository.save_action(item, action)
                if command == "new" and "reply" not in action:
                    action["reply"] = "New session created."
            elif command == "close":
                if not action.get("close_activity_id"):
                    action["close_activity_id"] = str(uuid4())
                    await self.repository.save_action(item, action)
                    await self.sessions.close_session(
                        session_id, activity_id=UUID(action["close_activity_id"])
                    )
                    await self.remember_reference(item, action, "close", session_id)
                else:
                    execution = await self.sessions.get_activity(
                        session_id,
                        UUID(action["close_activity_id"]),
                        run_id=action.get("close_run_id"),
                    )
                    action["close_run_id"] = execution.run_id
                    await self.repository.save_action(item, action)
                    await self.sessions.wait_session_activity(
                        session_id,
                        execution.activity_id,
                        run_id=execution.run_id,
                        activity_type=CLOSE,
                    )
                action["reply"] = "Session closed."
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
        except RPCError as error:
            # Dispatch may have succeeded. Keep the resolved IDs and frozen intent
            # so the inbox confirms the same operation after a transient outage.
            if error.status == RPCStatusCode.NOT_FOUND:
                await self.unconfirmed(item)
            else:
                await self.repository.save_action(
                    item, item.resolved_action or {}, next_attempt_at=time.time() + 1
                )
        except SessionExecutionError as error:
            action = dict(item.resolved_action or {})
            if error.run_id:
                for kind in ("create", "run", "close"):
                    if action.get(kind + "_activity_id") == str(error.activity_id):
                        action[kind + "_run_id"] = error.run_id
            if error.category == "OutcomeUnavailable":
                await self.repository.save_action(item, action)
                await self.unconfirmed(item)
                return True
            if error.category == "UnableToConfirm":
                await self.repository.save_action(item, action, next_attempt_at=time.time() + 1)
                return True
            await self.repository.save_action(
                item,
                action
                | {
                    "command": "reply",
                    "reply": (
                        f"Session: {error.session_id}\nActivity: {error.activity_id}\n"
                        f"Execution failed ({error.category}). Use /close {error.session_id} "
                        "to clean up this session or /new to continue."
                    ),
                },
            )
        except ActivityIdentityConflict:
            await self.unconfirmed(item)
        except LifecycleError:
            await self.repository.save_action(
                item,
                {
                    "command": "reply",
                    "reply": "Session is closing or closed. Use /new to continue.",
                },
            )
        except (LookupError, ValueError) as error:
            # Definite business validation failures are safe to report by category only.
            if isinstance(error, LookupError) and any(
                (item.resolved_action or {}).get(kind + "_activity_id")
                for kind in ("create", "run", "close")
            ):
                await self.unconfirmed(item)
            else:
                await self.repository.save_action(
                    item,
                    (item.resolved_action or {})
                    | {
                        "command": "reply",
                        "reply": f"Session operation failed ({type(error).__name__}).",
                    },
                )
        return True

    async def unconfirmed(self, item: InboxRow) -> None:
        action = dict(item.resolved_action or {})
        target = action.get("session_id") or action.get("creating_session_id")
        identities = "\n".join(
            f"{kind.title()} Activity: {action[kind + '_activity_id']}"
            for kind in ("create", "run", "close")
            if action.get(kind + "_activity_id")
        )
        await self.repository.save_action(
            item,
            action
            | {
                "command": "reply",
                "completed": True,
                "reply": (
                    f"Session: {target}\n{identities}\nOutcome cannot be confirmed. "
                    "Use /new or /close <session UUID>."
                ),
            },
        )

    async def process(self) -> None:
        while True:
            try:
                if not await self.process_once():
                    await asyncio.sleep(0.25)
            except SQLAlchemyError:
                logger.warning("Telegram input storage unavailable")
                await asyncio.sleep(1)

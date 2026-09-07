"""Authorized control operations, thin adapters over owner services."""

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID, uuid5
from weakref import WeakValueDictionary

import psycopg
from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python

from kapy.agent import AgentResourceLimit
from kapy.rpc import JsonObject, JsonValue, RpcError
from kapy.settings import Settings
from kapy.state import (
    Conflict,
    InvalidArgument,
    NotFound,
    QueryLimitExceeded,
    RunContext,
    RunResult,
    ServiceUnavailable,
    SessionSpec,
    StateError,
    UnsafeQuery,
)

from . import params as p
from .auth import Principal, denied
from .storage import Metadata

if TYPE_CHECKING:
    from kapy.agent import AgentPayloadStore, Runner
    from kapy.skills import SkillService
    from kapy.state import SessionService

    from .machines import MachineRegistry

logger = logging.getLogger(__name__)


def plain(value: Any) -> JsonValue:
    return cast(JsonValue, to_jsonable_python(value))


MODELS: dict[str, type[BaseModel]] = {
    "session.create": p.Create,
    "session.get": p.SessionId,
    "session.list": p.ListSessions,
    "session.update": p.Update,
    "session.delete": p.Mutation,
    "session.input": p.Input,
    "session.output": p.Output,
    "session.wait": p.Wait,
    "event.publish": p.Publish,
    "history.read": p.Read,
    "history.search": p.Search,
    "history.query": p.Query,
    "history.export": p.Export,
    "skill.list": p.SkillList,
    "skill.get": p.SkillId,
    "skill.read": p.SkillId,
    "skill.create": p.SkillTransfer,
    "skill.update": p.SkillUpdate,
    "skill.download": p.SkillDownload,
    "skill.delete": p.SkillDelete,
}
MUTATIONS = {
    "session.create",
    "session.update",
    "session.delete",
    "session.input",
    "event.publish",
    "skill.create",
    "skill.update",
    "skill.delete",
    "skill.download",
}
ERRORS: dict[type[StateError], tuple[int, str]] = {
    NotFound: (-32004, "not_found"),
    Conflict: (-32009, "conflict"),
    InvalidArgument: (-32602, "invalid_argument"),
    UnsafeQuery: (-32040, "unsafe_query"),
    QueryLimitExceeded: (-32041, "query_limit"),
    ServiceUnavailable: (-32030, "unavailable"),
}


class ControlService:
    def __init__(
        self,
        *,
        settings: Settings,
        metadata: Metadata,
        sessions: SessionService,
        skills: SkillService,
        runner: Runner,
        machines: MachineRegistry,
        payload_store: AgentPayloadStore,
    ) -> None:
        self.settings = settings
        self.metadata = metadata
        self.sessions = sessions
        self.skills = skills
        self.runner = runner
        self.machines = machines
        self.payload_store = payload_store
        self.skill_slots = asyncio.Semaphore(2)
        self._locks: WeakValueDictionary[UUID, asyncio.Lock] = WeakValueDictionary()

    async def call(
        self,
        method: str,
        params: JsonObject,
        *,
        principal: Principal,
    ) -> JsonValue:
        model = MODELS.get(method)
        if model is None:
            raise RpcError(-32601, "Method not found")
        try:
            parsed = model.model_validate_json(json.dumps(params, allow_nan=False))
        except ValidationError, ValueError, TypeError:
            raise RpcError(-32602, "Invalid method parameters") from None
        data = parsed.model_dump()
        canonical = cast(JsonObject, parsed.model_dump(mode="json"))
        try:
            # Even direct plugin calls must enforce a live caller identity.
            if principal.kind == "session":
                caller = await self.sessions.get_session(cast(UUID, principal.session_id))
                if principal.machine_id not in caller.machine_ids:
                    raise denied("Caller is not associated with this machine")
            if method in MUTATIONS:
                request_id: UUID = data["request_id"]
                lock = self._locks.setdefault(request_id, asyncio.Lock())
                async with lock:
                    previous = await self.metadata.request(request_id)
                    if previous is None:
                        await self._authorize(method, data, principal)
                    request = await self.metadata.reserve(
                        request_id,
                        principal,
                        method,
                        canonical,
                        data.get("session_id"),
                    )
                    if request["result"] is not None:
                        return request["result"]
                    if previous is not None:
                        await self._authorize(method, data, principal)
                    result = await self._dispatch(method, data, principal, request)
                    # Create adapters commit authorization and their result together.
                    if method not in {"session.create", "skill.create"}:
                        await self.metadata.finish(request_id, result)
                    return result
            await self._authorize(method, data, principal)
            return await self._dispatch(method, data, principal, None)
        except AgentResourceLimit:
            raise RpcError(
                -32020,
                "Initial session state exceeds its limit",
                {
                    "kind": "resource_limit",
                },
            ) from None
        except StateError as exc:
            code, kind = ERRORS.get(type(exc), (-32030, "unavailable"))
            raise RpcError(code, kind.replace("_", " "), {"kind": kind}) from None

    async def _authorize(self, method: str, data: dict[str, Any], principal: Principal) -> None:
        session_id = data.get("session_id")
        if session_id is not None:
            await self.metadata.authorize(principal, session_id)
        if method == "session.wait":
            request = await self.metadata.request(data["request_id"])
            if not request or request["target_session_id"] != session_id:
                raise denied("Unknown request for this session")
            if principal.kind != "operator" and request["principal_id"] != principal.id:
                raise denied("Request belongs to another caller")
        if method in {"session.create", "session.update"}:
            machines = data["machine_ids"]
            if len(set(machines)) != len(machines) or any(
                item not in self.settings.machine_tokens for item in machines
            ):
                raise InvalidArgument("Unknown or duplicate machines")
            if (
                data["default_machine_id"] is not None
                and data["default_machine_id"] not in machines
            ):
                raise InvalidArgument("Default machine must be associated")
            if principal.kind == "session":
                caller = await self.sessions.get_session(cast(UUID, principal.session_id))
                if not set(machines) <= set(caller.machine_ids):
                    raise denied("Child machines must be a subset of caller machines")
            config = data["config"]
            if set(config) - {"model", "instructions"} or any(
                not isinstance(value, str) for value in config.values()
            ):
                raise InvalidArgument("Config accepts model and instructions strings")
        if method == "event.publish":
            await self.metadata.channel(data["waiting_id"], principal, publish=True)
        if method in {"skill.update", "skill.delete"} and principal.kind != "operator":
            rows = await self.metadata.rows(
                "SELECT creator_principal FROM gateway_skill_access WHERE skill_id=%s",
                (data["skill_id"],),
            )
            if not rows or rows[0]["creator_principal"] != principal.id:
                raise denied("Only the skill creator may change it")

    async def completion_channel(
        self,
        request_id: UUID,
        supplied: UUID | None,
        principal: Principal,
        target: UUID | None = None,
    ) -> UUID:
        channel = supplied or uuid5(request_id, "completion-channel")
        await self.metadata.channel(channel, principal, create=supplied is None, subscribe=True)
        await self.metadata.grant(channel, principal.id, publish=False, subscribe=True)
        if target is not None:
            await self.metadata.grant(channel, f"session:{target}", publish=True, subscribe=False)
        return channel

    async def _dispatch(
        self,
        method: str,
        data: dict[str, Any],
        principal: Principal,
        request: dict[str, Any] | None,
    ) -> JsonValue:
        if method.startswith("skill."):
            from .skills import dispatch_skill

            return await dispatch_skill(self, method, data, principal, request)
        sid = cast(UUID, data.get("session_id"))
        if method == "session.create":
            channel = await self.completion_channel(
                data["request_id"],
                data["waiting_id"],
                principal,
            )
            initial = self.runner.initial_state(
                instructions=data["config"].get("instructions", ""),
                skills=await self.skills.catalog(),
            )
            spec = SessionSpec(
                data["title"],
                data["machine_ids"],
                data["default_machine_id"],
                data["config"],
                initial,
            )
            created = await self.sessions.create_session(
                spec,
                request_id=data["request_id"],
                input=data["input"],
                mode=data["mode"],
                waiting_id=channel,
            )
            sid = created.session.id
            await self.metadata.channel(sid, principal, create=True)
            await self.metadata.grant(sid, f"session:{sid}", publish=True, subscribe=True)
            await self.metadata.grant(channel, f"session:{sid}", publish=True, subscribe=False)
            owner = principal.id
            if principal.kind == "session":
                access = await self.metadata.authorize(principal, cast(UUID, principal.session_id))
                owner = access["owner_id"]
            result = plain(created)
            await self.metadata.finish(
                data["request_id"],
                result,
                target=sid,
                owner=owner,
                parent=principal.session_id,
            )
            self.machines.prepare(sid, created.session.machine_ids)
            return result
        if method == "session.get":
            return plain(await self.sessions.get_session(sid))
        if method == "session.list":
            return plain(
                await self.sessions.list_sessions(
                    session_ids=await self.metadata.visible(principal),
                    **data,
                )
            )
        if method == "session.update":
            session = await self.sessions.update_session(**data)
            self.machines.prepare(session.id, session.machine_ids)
            return plain(session)
        if method == "session.delete":
            return {"deleted": await self._delete(sid, data["request_id"])}
        if method == "session.input":
            data["waiting_id"] = await self.completion_channel(
                data["request_id"],
                data["waiting_id"],
                principal,
                sid,
            )
            return plain(await self.sessions.submit_input(**data))
        if method == "session.output":
            return plain(await self.sessions.read_output(**data))
        if method == "session.wait":
            return plain(await self.sessions.wait_submission(**data))
        if method == "event.publish":
            return plain(
                await self.sessions.publish_event(
                    **data,
                    producer_session_id=principal.session_id,
                )
            )
        if method == "history.read":
            return plain(await self.sessions.read_history(**data))
        if method == "history.search":
            return plain(await self.sessions.search_history(**data))
        if method == "history.query":
            return plain(await self.sessions.query_history(**data))
        if method == "history.export":
            return plain(await self.sessions.export_history(**data))
        raise RpcError(-32601, "Method not found")

    async def _delete(self, session_id: UUID, request_id: UUID) -> bool:
        rows = await self.metadata.rows(
            "SELECT * FROM gateway_session_cleanup WHERE session_id=%s", (session_id,)
        )
        if not rows:
            session = await self.sessions.get_session(session_id)
            await self.metadata.begin_cleanup(session_id, request_id, session.machine_ids)
        deleted = await self.sessions.delete_session(session_id, request_id=request_id)
        await self.metadata.mark_deleted(session_id)
        return deleted

    async def authorize_channels(
        self,
        session_id: UUID,
        waiting_ids: tuple[UUID, ...],
        *,
        action: Literal["publish", "subscribe"],
    ) -> None:
        principal = Principal("session", session_id=session_id)
        try:
            for waiting_id in waiting_ids:
                await self.metadata.channel(
                    waiting_id,
                    principal,
                    publish=action == "publish",
                    subscribe=action == "subscribe",
                )
        except RpcError as exc:
            raise PermissionError("Channel access denied") from exc

    async def authorize_wait(self, session_id: UUID, waiting_ids: tuple[UUID, ...]) -> None:
        await self.authorize_channels(session_id, waiting_ids, action="subscribe")

    async def grant_channel(
        self,
        waiting_id: UUID,
        target_session_id: UUID,
        *,
        can_publish: bool,
        can_subscribe: bool,
        principal: Principal,
    ) -> None:
        await self.metadata.authorize(principal, target_session_id)
        rows = await self.metadata.rows(
            "SELECT creator_principal FROM gateway_channels WHERE waiting_id=%s",
            (waiting_id,),
        )
        if principal.kind != "operator" and (
            not rows or rows[0]["creator_principal"] != principal.id
        ):
            raise denied("Only the channel creator may grant access")
        await self.metadata.grant(
            waiting_id,
            f"session:{target_session_id}",
            publish=can_publish,
            subscribe=can_subscribe,
        )

    async def run(self, context: RunContext) -> RunResult:
        while await self.metadata.access(context.session.id) is None:  # noqa: ASYNC110 - durable gate
            await asyncio.sleep(0.05)
        result = await self.runner(context)
        await self.authorize_wait(context.session.id, result.wait_for)
        return result

    async def recover(self) -> None:
        """Replay only fixed State mutations; machine side effects are never retried here."""
        rows = await self.metadata.rows(
            "SELECT * FROM gateway_requests WHERE result IS NULL AND left(method,8)='session.'"
        )
        for row in rows:
            principal_id = row["principal_id"]
            if principal_id == "operator":
                principal = Principal("operator")
            elif principal_id.startswith("telegram:"):
                principal = Principal(
                    "telegram",
                    telegram_route=cast(
                        tuple[int, int, int], tuple(map(int, principal_id.split(":")[1:]))
                    ),
                )
            else:
                # Replay a previously authorized durable intent, even if its caller was deleted.
                principal = Principal("session", session_id=UUID(principal_id.split(":")[1]))
            lock = self._locks.setdefault(row["request_id"], asyncio.Lock())
            async with lock:
                current = await self.metadata.request(row["request_id"])
                if current is None or current["result"] is not None:
                    continue
                try:
                    model = MODELS[row["method"]].model_validate_json(json.dumps(row["params"]))
                    result = await self._dispatch(row["method"], model.model_dump(), principal, row)
                    if row["method"] != "session.create":
                        await self.metadata.finish(row["request_id"], result)
                except StateError, RpcError:
                    logger.warning("Gateway recovery pending for %s", row["request_id"])

    async def cleanup_once(self) -> None:
        for row in await self.metadata.rows(
            "SELECT * FROM gateway_session_cleanup WHERE state <> 'complete'"
        ):
            sid = row["session_id"]
            try:
                if row["state"] == "deleting":
                    await self._delete(sid, row["request_id"])
                if row["payload_pending"]:
                    await self.payload_store.delete_session(sid)
                    await self.metadata.rows(
                        "UPDATE gateway_session_cleanup SET payload_pending=false "
                        "WHERE session_id=%s",
                        (sid,),
                    )
                pending = list(row["pending_machine_ids"])
                for machine_id in tuple(pending):
                    released = await self.machines.release(machine_id, sid)
                    if released:
                        pending.remove(machine_id)
                        await self.metadata.rows(
                            "UPDATE gateway_session_cleanup SET pending_machine_ids=%s "
                            "WHERE session_id=%s",
                            (json.dumps(pending), sid),
                        )
                if not pending:
                    await self.metadata.rows(
                        "UPDATE gateway_session_cleanup SET state='complete' WHERE session_id=%s",
                        (sid,),
                    )
            except StateError, RpcError, OSError, psycopg.Error:
                logger.warning("Session cleanup remains pending for %s", sid)

    async def background(self) -> None:
        while True:
            try:
                await self.recover()
                await self.cleanup_once()
            except psycopg.Error, OSError:
                logger.warning("Gateway metadata unavailable; recovery will retry")
            await asyncio.sleep(1)

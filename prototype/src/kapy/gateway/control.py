"""Authorized control operations, thin adapters over owner services."""

import asyncio
import json
import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Literal, cast
from uuid import UUID
from weakref import WeakValueDictionary

import httpx2
import psycopg
from pydantic import BaseModel, SecretStr, ValidationError
from pydantic_core import to_jsonable_python

from kapy.agent import (
    AgentResourceLimit,
    ModelBackendFactory,
    Runner,
    RunnerConfig,
    ScriptTool,
    create_model_backend,
)
from kapy.rpc import JsonObject, JsonValue, RpcError
from kapy.settings import Settings
from kapy.state import (
    Conflict,
    InvalidArgument,
    NotFound,
    QueryLimitExceeded,
    RunContext,
    RunFailure,
    RunnerState,
    RunResult,
    ServiceUnavailable,
    SessionSpec,
    StateError,
    UnsafeQuery,
    WaitFor,
)

from . import params as p
from .auth import Principal, Rejected, denied
from .models import Providers, invalid, parse_session_model, public_provider
from .storage import Metadata

if TYPE_CHECKING:
    from kapy.agent import AgentPayloadStore
    from kapy.skills import SkillService
    from kapy.state import SessionService

    from .machines import MachineRegistry

logger = logging.getLogger(__name__)


def plain(value: Any) -> JsonValue:
    return cast(JsonValue, to_jsonable_python(value))


MODELS: dict[str, type[BaseModel]] = {
    "provider.create": p.ProviderCreate,
    "provider.get": p.ProviderId,
    "provider.list": p.ProviderList,
    "provider.update": p.ProviderUpdate,
    "provider.delete": p.ProviderDelete,
    "provider.discover": p.ProviderDiscover,
    "provider.models": p.ProviderModels,
    "provider.model.create": p.ModelCreate,
    "provider.model.get": p.ModelId,
    "provider.model.update": p.ModelUpdate,
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


def request_error(exc: StateError | AgentResourceLimit | RpcError) -> RpcError:
    if isinstance(exc, RpcError):
        return exc
    if isinstance(exc, AgentResourceLimit):
        return Rejected(
            -32020, "Initial session state exceeds its limit", {"kind": "resource_limit"}
        )
    code, kind = ERRORS.get(type(exc), (-32030, "unavailable"))
    error_type = (
        Rejected
        if isinstance(
            exc,
            (
                NotFound,
                Conflict,
                InvalidArgument,
                UnsafeQuery,
                QueryLimitExceeded,
            ),
        )
        else RpcError
    )
    return error_type(code, kind.replace("_", " "), {"kind": kind})


class ControlService:
    def __init__(
        self,
        *,
        settings: Settings,
        metadata: Metadata,
        sessions: SessionService,
        skills: SkillService,
        http_client: httpx2.AsyncClient,
        model_backend_factory: ModelBackendFactory = create_model_backend,
        plugins: Sequence[ScriptTool] = (),
        machines: MachineRegistry,
        payload_store: AgentPayloadStore,
    ) -> None:
        self.settings = settings
        self.metadata = metadata
        self.sessions = sessions
        self.skills = skills
        self.http_client = http_client
        self.model_backend_factory = model_backend_factory
        self.plugins = tuple(plugins)
        self.providers = Providers(metadata, http_client)
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
        if isinstance(data.get("api_key"), SecretStr):
            canonical["api_key"] = data["api_key"].get_secret_value()
        try:
            # Even direct plugin calls must enforce a live caller identity.
            if principal.kind == "session":
                caller = await self.sessions.get_session(cast(UUID, principal.session_id))
                if principal.machine_id not in caller.machine_ids:
                    raise denied("Caller is not associated with this machine")
            if method.startswith("provider."):
                await self._authorize(method, data, principal)
                if "request_id" in data:
                    lock = self._locks.setdefault(data["request_id"], asyncio.Lock())
                    async with lock:
                        return plain(
                            await self.providers.mutate(method, data, canonical, principal)
                        )
                return await self._provider_read(method, data)
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
                    if request["error"] is not None:
                        raise RpcError(**request["error"])
                    if request["result"] is not None:
                        return request["result"]
                    try:
                        if previous is not None:
                            await self._authorize(method, data, principal)
                        result = await self._dispatch(method, data, principal, request)
                        # Create adapters commit authorization and their result together.
                        if method not in {"session.create", "skill.create"}:
                            await self.metadata.finish(request_id, result)
                        return result
                    except (StateError, AgentResourceLimit, RpcError) as exc:
                        error = request_error(exc)
                        if isinstance(error, Rejected):
                            await self.metadata.reject(request_id, error)
                        raise error from None
            await self._authorize(method, data, principal)
            return await self._dispatch(method, data, principal, None)
        except (AgentResourceLimit, StateError) as exc:
            raise request_error(exc) from None

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
            if set(config) - {"model", "instructions", "output_mode"} or not isinstance(
                config.get("instructions", ""), str
            ):
                raise invalid("Config accepts model object, instructions and output_mode")
            if config.get("output_mode", "text") not in ("text", "reply_to"):
                raise invalid("output_mode must be text or reply_to")
            supplied_model = config.get("model")
            if supplied_model is not None and (
                not isinstance(supplied_model, dict)
                or set(supplied_model) - {"model_id", "context_window_tokens", "max_output_tokens"}
            ):
                raise invalid("Model accepts model_id and token budget overrides only")
            if principal.kind == "session" and config.get("model") is not None:
                supplied = config["model"]
                if not isinstance(supplied, dict):
                    raise invalid("config.model must be an object")
                selected = supplied.get("model_id")
                if selected is not None:
                    try:
                        selected_id = UUID(selected)
                    except ValueError, TypeError, AttributeError:
                        raise invalid("model_id must be a UUID") from None
                    await self._bound_provider(principal, model_id=selected_id)
        if method.startswith("provider.") and principal.kind == "session":
            if method not in {"provider.get", "provider.models", "provider.model.get"}:
                raise denied("Session capabilities cannot manage or enumerate providers")
            await self._bound_provider(
                principal, provider_id=data.get("provider_id"), model_id=data.get("model_id")
            )
        if method == "event.publish":
            await self.metadata.channel(data["waiting_id"], principal, publish=True)
        if method in {"skill.update", "skill.delete"} and principal.kind != "operator":
            rows = await self.metadata.rows(
                "SELECT creator_principal FROM gateway_skill_access WHERE skill_id=%s",
                (data["skill_id"],),
            )
            if not rows or rows[0]["creator_principal"] != principal.id:
                raise denied("Only the skill creator may change it")

    async def _bound_provider(
        self, principal: Principal, *, provider_id: UUID | None = None, model_id: UUID | None = None
    ) -> None:
        caller = await self.sessions.get_session(cast(UUID, principal.session_id))
        current = parse_session_model(caller.config.get("model"))
        bound = await self.providers.model(current.model_id)
        if model_id is not None:
            selected = await self.providers.model(model_id)
            provider_id = selected["provider_id"]
        if provider_id != bound["provider_id"]:
            raise denied("Provider is not bound to the calling session")

    async def _provider_read(self, method: str, data: dict) -> JsonValue:
        if method == "provider.get":
            return plain(public_provider(await self.providers.get(data["provider_id"])))
        if method == "provider.model.get":
            model = await self.providers.model(data["model_id"])
            await self.providers.get(model["provider_id"])
            return plain(model)
        if method == "provider.models":
            await self.providers.get(data["provider_id"])
            rows = await self.metadata.rows(
                "SELECT * FROM gateway_provider_models WHERE provider_id=%s AND (%s::uuid IS "
                "NULL OR id>%s) ORDER BY id LIMIT %s",
                (data["provider_id"], data["after_id"], data["after_id"], data["limit"] + 1),
            )
            complete = await self.metadata.rows(
                "SELECT id FROM gateway_provider_models WHERE provider_id=%s ORDER BY id LIMIT 2",
                (data["provider_id"],),
            )
            return plain(
                {
                    "items": rows[: data["limit"]],
                    "next_after_id": rows[data["limit"] - 1]["id"]
                    if len(rows) > data["limit"]
                    else None,
                    "default_model_id": complete[0]["id"] if len(complete) == 1 else None,
                }
            )
        rows = await self.metadata.rows(
            "SELECT * FROM gateway_providers WHERE NOT deleted AND (%s::uuid IS NULL OR id>%s) "
            "ORDER BY id LIMIT %s",
            (data["after_id"], data["after_id"], data["limit"] + 1),
        )
        return plain(
            {
                "items": [public_provider(row) for row in rows[: data["limit"]]],
                "next_after_id": rows[data["limit"] - 1]["id"]
                if len(rows) > data["limit"]
                else None,
            }
        )

    async def completion_channel(
        self,
        channel: UUID,
        principal: Principal,
        target: UUID,
    ) -> None:
        await self.metadata.register_channel(
            channel,
            producer=f"session:{target}",
            receiver=principal.id,
        )

    async def _dispatch(
        self,
        method: str,
        data: dict[str, Any],
        principal: Principal,
        request: dict[str, Any] | None,
    ) -> JsonValue:
        if method in {"session.create", "session.update"}:
            assert request is not None
            operation = request["operation"]
            if "config" not in operation:
                inherited = None
                if method == "session.create" and principal.kind == "session":
                    caller = await self.sessions.get_session(cast(UUID, principal.session_id))
                    candidate = caller.config.get("model")
                    inherited = candidate if isinstance(candidate, dict) else None
                resolved = await self.providers.resolve(data["config"].get("model"), inherited)
                operation["config"] = {**data["config"], "model": resolved}
                await self.metadata.operation(data["request_id"], operation)
            data = {**data, "config": operation["config"]}
        if method.startswith("skill."):
            from .skills import dispatch_skill

            return await dispatch_skill(self, method, data, principal, request)
        sid = cast(UUID, data.get("session_id"))
        if method == "session.create":
            assert request is not None
            operation = request["operation"]
            if "initial_state" not in operation:
                initial = Runner.initial_state(
                    instructions=data["config"].get("instructions", ""),
                    skills=await self.skills.catalog(),
                )
                operation["initial_state"] = plain(initial)
                await self.metadata.operation(data["request_id"], operation)
            snapshot = operation["initial_state"]
            initial = RunnerState(codec=snapshot["codec"], data=snapshot["data"])
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
                receiver_session_id=principal.session_id,
            )
            sid = created.session.id
            if created.submission is not None:
                await self.completion_channel(created.submission.waiting_id, principal, sid)
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
            async with self.machines.session_lock(sid):
                session = await self.sessions.update_session(**data)
                self.machines.prepare(session.id, session.machine_ids)
                return plain(session)
        if method == "session.delete":
            return {"deleted": await self._delete(sid, data["request_id"])}
        if method == "session.input":
            submission = await self.sessions.submit_input(
                **data,
                receiver_session_id=principal.session_id,
            )
            await self.completion_channel(submission.waiting_id, principal, sid)
            return plain(submission)
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
        async with self.machines.session_lock(session_id):
            rows = await self.metadata.rows(
                "SELECT * FROM gateway_session_cleanup WHERE session_id=%s", (session_id,)
            )
            if not rows:
                session = await self.sessions.get_session(session_id)
                resources = await self.metadata.rows(
                    "SELECT machine_id FROM gateway_machine_resources WHERE session_id=%s",
                    (session_id,),
                )
                machines = sorted(
                    set(session.machine_ids) | {row["machine_id"] for row in resources}
                )
                await self.metadata.begin_cleanup(session_id, request_id, machines)
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
            "SELECT producer_principal FROM gateway_channels WHERE waiting_id=%s",
            (waiting_id,),
        )
        if principal.kind != "operator" and (
            not rows or rows[0]["producer_principal"] != principal.id
        ):
            raise denied("Only the channel producer may grant access")
        await self.metadata.grant(
            waiting_id,
            f"session:{target_session_id}",
            publish=can_publish,
            subscribe=can_subscribe,
        )

    async def run(self, context: RunContext) -> RunResult:
        while await self.metadata.access(context.session.id) is None:  # noqa: ASYNC110 - durable gate
            await asyncio.sleep(0.05)
        try:
            selected = parse_session_model(context.session.config.get("model"))
            provider, model, window, output = await self.providers.effective(selected)
        except Rejected as exc:
            code = "model_unavailable" if exc.code == -32004 else "model_configuration"
            raise RunFailure(code, exc.message) from None
        except Exception:
            raise RuntimeError("Model configuration is unavailable; retry later") from None
        try:
            identity = json.dumps(
                [
                    str(provider["id"]),
                    provider["revision"],
                    provider["type"],
                    provider["base_url"],
                    str(model["id"]),
                    model["name"],
                ]
            )
            runner = Runner(
                RunnerConfig(
                    model=model["name"],
                    context_window_tokens=window,
                    max_output_tokens=output,
                    compression_ratio=self.settings.compression_ratio,
                    keep_recent_ratio=self.settings.keep_recent_ratio,
                    media_max_bytes=self.settings.media_max_bytes,
                ),
                self.machines,
                model_backend=self.model_backend_factory(
                    self.providers.connection(provider), self.http_client
                ),
                payload_store=self.payload_store,
                authorize_wait=self.authorize_wait,
                plugins=self.plugins,
                model_identity=identity,
            )
            result = await runner(context)
        except Exception:
            raise RuntimeError(
                "Model execution failed; check model configuration or retry"
            ) from None
        if isinstance(result.output, WaitFor):
            await self.authorize_wait(context.session.id, result.output.waiting_ids)
        return result

    async def recover(self) -> None:
        """Replay only fixed State mutations; machine side effects are never retried here."""
        rows = await self.metadata.rows(
            "SELECT * FROM gateway_requests WHERE result IS NULL AND error IS NULL "
            "AND left(method,8)='session.'"
        )
        for row in rows:
            # Replay a previously authorized durable intent, even if its caller was deleted.
            principal = Principal.from_id(row["principal_id"])
            lock = self._locks.setdefault(row["request_id"], asyncio.Lock())
            async with lock:
                current = await self.metadata.request(row["request_id"])
                if current is None or current["result"] is not None or current["error"] is not None:
                    continue
                try:
                    model = MODELS[row["method"]].model_validate_json(json.dumps(row["params"]))
                    result = await self._dispatch(
                        row["method"], model.model_dump(), principal, current
                    )
                    if row["method"] != "session.create":
                        await self.metadata.finish(row["request_id"], result)
                except (StateError, RpcError, AgentResourceLimit) as exc:
                    error = request_error(exc)
                    if isinstance(error, Rejected):
                        await self.metadata.reject(row["request_id"], error)
                    else:
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

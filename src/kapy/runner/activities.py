"""Worker-owned session Activities; native Temporal executions own all resources.

Only executing Workers acquire database ownership. The complete native identity
fences progress; Temporal alone owns scheduling, timeouts, outcomes and inputs.
No model/tool/hook or Temporal RPC runs inside an ownership transaction.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import cast
from uuid import UUID

import anyio
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.settings import ModelSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from kapy.agent_output import AgentOutputService
from kapy.control.models.repository import ModelRepository
from kapy.control.models.runtime import (
    build_model,
    build_provider,
    resolve_classes,
    validate_settings,
)
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.control.sessions.temporal import TERMINAL_STATUSES, failure_category
from kapy.control.sessions.types import (
    CreateSessionActivity,
    OwnershipLost,
    RunAgentActivity,
    SessionActivityRequest,
    SessionBusy,
    SessionRecord,
)
from kapy.layers.registry import layer_registry
from kapy.lifecycle import LifecycleStatus
from pydantic_ai_layer_protocol import dump_object

from .agent import agent
from .output import handle_deltas
from .scopes import check_owner, close_scopes, create_scopes
from .types import ExecutionConfig, RunnerDeps, RunnerInput


class RunnerActivity:
    """Borrow Worker resources; each invocation owns clients, State and heartbeats."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        output_service: AgentOutputService,
        temporal_client: Client,
    ) -> None:
        self._session_factory = session_factory
        self._output_service = output_service
        self._temporal_client = temporal_client

    @asynccontextmanager
    async def _invocation(self, session_id: UUID):
        info = activity.info()
        if info.in_workflow or not info.activity_run_id:
            raise ApplicationError("StandaloneIdentityRequired", non_retryable=True)
        activity_id, run_id = UUID(info.activity_id), info.activity_run_id
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            yield activity_id, run_id
        except BaseException as error:
            # Resource contexts have exited before failure releases the writer.
            # A lost success-commit reply cannot match the cleared identity.
            with anyio.move_on_after(15, shield=True):
                try:
                    async with self._session_factory() as db, db.begin():
                        await SessionRepository(db).complete(
                            session_id,
                            activity_id,
                            run_id,
                            (
                                LifecycleStatus.CREATING,
                                LifecycleStatus.READY,
                                LifecycleStatus.CLOSING,
                            ),
                            LifecycleStatus.CLOSING,
                        )
                except OwnershipLost:
                    pass
                except Exception:
                    activity.logger.warning("Unable to release session writer after failure")
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ApplicationError(
                failure_category(error),
                type=failure_category(error),
                non_retryable=True,
            ) from None
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    @staticmethod
    async def _heartbeat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(2)

    @activity.defn(name="kapy.create_session")
    async def create(self, request: CreateSessionActivity) -> SessionRecord:
        async with self._invocation(request.session_id) as (activity_id, run_id):
            if request.request.id is not None and request.request.id != request.session_id:
                raise ValueError("Creation identities must agree")
            # All Config/default validation precedes insertion and every enter.
            configs = layer_registry.prepare_configs(request.request.layers)
            for name in configs:
                definition = layer_registry.definitions[name]
                if definition.enter_scope is None:
                    definition.state_type.model_validate({})
            row = SessionRow(
                **request.request.model_dump(exclude={"id", "layers"}),
                id=request.session_id,
                layers={name: dump_object(config) for name, config in configs.items()},
                layer_states={},
                status=LifecycleStatus.CREATING,
                current_activity_id=activity_id,
                current_activity_run_id=run_id,
            )
            async with self._session_factory() as db, db.begin():
                await SessionRepository(db).create(row)
            return await create_scopes(
                self._session_factory,
                activity_id,
                run_id,
                request.session_id,
                configs,
            )

    async def _claim_close(self, session_id: UUID, activity_id: UUID, run_id: str) -> SessionRow:
        async with self._session_factory() as db, db.begin():
            repo = SessionRepository(db)
            row = await repo._session(session_id)
            if row.current_activity_id is None:
                if row.status == LifecycleStatus.CLOSED:
                    return row
                claimed = await repo.claim_close_or_run(session_id, activity_id, run_id, close=True)
                if claimed is not None:
                    return claimed
                raise SessionBusy("Session cannot be closed")
            assert row.current_activity_run_id is not None
            previous = (row.current_activity_id, row.current_activity_run_id, row.status)
        # Exact native lookup is restricted to explicit close; queries and run
        # admission never release an abandoned writer.
        try:
            description = await self._temporal_client.get_activity_handle(
                str(previous[0]),
                run_id=previous[1],
            ).describe(rpc_timeout=timedelta(seconds=10))
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise ApplicationError("UnableToConfirm", type="UnableToConfirm") from None
        else:
            if description.status not in TERMINAL_STATUSES:
                raise SessionBusy("Previous native execution may still be active")
        async with self._session_factory() as db, db.begin():
            repo = SessionRepository(db)
            claimed = await repo.claim_close_or_run(
                session_id,
                activity_id,
                run_id,
                close=True,
                previous=previous,
            )
            if claimed is not None:
                return claimed
            row = await repo._session(session_id)
            if row.current_activity_id is None:
                if row.status == LifecycleStatus.CLOSED:
                    return row
                claimed = await repo.claim_close_or_run(session_id, activity_id, run_id, close=True)
                if claimed is not None:
                    return claimed
            raise SessionBusy("Another execution changed the session")

    @activity.defn(name="kapy.close_session")
    async def close(self, request: SessionActivityRequest) -> SessionRecord:
        async with self._invocation(request.session_id) as (activity_id, run_id):
            row = await self._claim_close(request.session_id, activity_id, run_id)
            if row.status == LifecycleStatus.CLOSED:
                return SessionRecord.model_validate(row)
            return await close_scopes(
                self._session_factory,
                activity_id,
                run_id,
                row.id,
                {"layers": row.layers, "layer_states": row.layer_states},
            )

    @activity.defn(name="kapy.run_agent")
    async def run(self, request: RunAgentActivity) -> str:
        async with self._invocation(request.session_id) as (activity_id, run_id):
            async with self._session_factory() as db, db.begin():
                session = await SessionRepository(db).claim_run(
                    request.session_id, activity_id, run_id
                )
                catalog = ModelRepository(db)
                model = await catalog.get_model(session.provider_id, session.model_name)
                provider = await catalog.get_provider_config(session.provider_id)
                _, model_class = resolve_classes(provider)
                settings = validate_settings(model_class, model.settings | session.model_settings)
                if session.runner_state is not None:
                    ModelMessagesTypeAdapter.validate_json(session.runner_state)
                data = RunnerInput(
                    session_id=session.id,
                    runner_state=session.runner_state,
                    layers=layer_registry.load(session.layers, session.layer_states),
                    user_prompt=request.user_prompt,
                    config=ExecutionConfig(
                        provider_class=provider.provider_class,
                        model_class=provider.model_class,
                        model_name=model.model_name,
                        api_key=provider.api_key.get_secret_value(),
                        base_url=provider.base_url,
                        provider_kwargs=provider.provider_kwargs,
                        model_settings=settings,
                        context_window=model.context_window,
                    ),
                )
            return await self._run(activity_id, run_id, data)

    async def _run(self, activity_id: UUID, run_id: str, data: RunnerInput) -> str:
        history = (
            []
            if data.runner_state is None
            else ModelMessagesTypeAdapter.validate_json(data.runner_state)
        )
        provider_config = data.config.provider_config()
        provider_class, model_class = resolve_classes(provider_config)
        deps = RunnerDeps(
            session_id=data.session_id,
            activity_id=activity_id,
            activity_run_id=run_id,
            session_factory=self._session_factory,
            output_service=self._output_service,
            layers=data.layers,
        )
        await check_owner(
            self._session_factory,
            data.session_id,
            activity_id,
            run_id,
            (LifecycleStatus.READY,),
        )
        async with (
            build_provider(provider_class, provider_config) as provider,
            build_model(
                model_class,
                data.config.model_name,
                provider,
                profile={"context_window": data.config.context_window},
            ) as model,
        ):
            result = await agent.run(
                data.user_prompt,
                model=model,
                model_settings=cast(ModelSettings, data.config.model_settings),
                deps=deps,
                message_history=history,
                event_stream_handler=handle_deltas,
            )
        await check_owner(
            self._session_factory,
            data.session_id,
            activity_id,
            run_id,
            (LifecycleStatus.READY,),
        )
        async with self._session_factory() as db, db.begin():
            await SessionRepository(db).complete(
                data.session_id,
                activity_id,
                run_id,
                (LifecycleStatus.READY,),
                LifecycleStatus.READY,
                runner_state=result.all_messages_json().decode("utf-8"),
                layer_states=layer_registry.snapshot(deps),
            )
        return result.output

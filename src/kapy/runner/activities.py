"""The standalone Agent Activity owns one run's runtime handles and writer.

Temporal owns scheduling, timeouts and outcomes. The native identity fences
progress, and RPCs, resource hooks and Agent calls run outside database transactions.
"""

import asyncio
from contextlib import asynccontextmanager, nullcontext, suppress
from typing import cast
from uuid import UUID

import anyio
from pydantic_ai.messages import ModelMessagesTypeAdapter, ModelResponse
from pydantic_ai.settings import ModelSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from kapy.agent_output import AgentOutputService
from kapy.control.models.repository import ModelRepository
from kapy.control.models.runtime import (
    build_model,
    build_provider,
    resolve_classes,
    validate_settings,
)
from kapy.control.sessions.repository import SessionRepository
from kapy.control.sessions.temporal import RUN, ensure_activity_terminal, failure_category
from kapy.control.sessions.types import OwnershipLost, RunAgentActivity, SessionBusy
from kapy.layers.registry import layer_registry
from kapy.lifecycle import LifecycleError, LifecycleStatus

from .agent import agent
from .output import handle_deltas
from .recording import check_owner
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
                        await SessionRepository(db).complete(session_id, activity_id, run_id)
                except OwnershipLost:
                    pass
                except Exception:
                    activity.logger.warning("Unable to release session writer after failure")
            if isinstance(error, asyncio.CancelledError):
                raise
            category = failure_category(error)
            raise ApplicationError(category, type=category, non_retryable=True) from None
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    @staticmethod
    async def _heartbeat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(2)

    @activity.defn(name=RUN)
    async def run(self, request: RunAgentActivity) -> str:
        async with self._invocation(request.session_id) as (activity_id, run_id):
            data = await self._claim_run(request, activity_id, run_id)
            return await self._run(activity_id, run_id, data)

    async def _claim_run(
        self, request: RunAgentActivity, activity_id: UUID, run_id: str
    ) -> RunnerInput:
        data = await self._try_claim_and_load(request, activity_id, run_id)
        if data is not None:
            return data
        async with self._session_factory.begin() as db:
            session = await SessionRepository(db).get_session(request.session_id)
        if session.status == LifecycleStatus.CLOSED:
            raise LifecycleError(f"Session {session.id} is closed")

        previous = None
        if session.current_activity_id is not None:
            assert session.current_activity_run_id is not None
            previous = (session.current_activity_id, session.current_activity_run_id)
            await ensure_activity_terminal(self._temporal_client, *previous)
        data = await self._try_claim_and_load(request, activity_id, run_id, previous=previous)
        if data is not None:
            return data
        async with self._session_factory.begin() as db:
            if await SessionRepository(db).get_status(session.id) == LifecycleStatus.CLOSED:
                raise LifecycleError(f"Session {session.id} is closed")
        raise SessionBusy(f"Session {session.id} is occupied")

    async def _try_claim_and_load(
        self,
        request: RunAgentActivity,
        activity_id: UUID,
        run_id: str,
        *,
        previous: tuple[UUID, str] | None = None,
    ) -> RunnerInput | None:
        """Claim and validate the execution inputs atomically so failures roll back the writer."""
        async with self._session_factory.begin() as db:
            session = await SessionRepository(db).claim_run(
                request.session_id, activity_id, run_id, previous=previous
            )
            if session is None:
                return None
            catalog = ModelRepository(db)
            model = await catalog.get_model(session.provider_id, session.model_name)
            provider = await catalog.get_provider_config(session.provider_id)
            _, model_class = resolve_classes(provider)
            settings = validate_settings(model_class, model.settings | session.model_settings)
            if session.runner_state is not None:
                ModelMessagesTypeAdapter.validate_json(session.runner_state)
            return RunnerInput(
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

    async def _run(self, activity_id: UUID, run_id: str, data: RunnerInput) -> str:
        history = (
            []
            if data.runner_state is None
            else ModelMessagesTypeAdapter.validate_json(data.runner_state)
        )
        tail = history[-1] if history else None
        history_only = data.user_prompt is None and (
            tail is None
            or (
                isinstance(tail, ModelResponse) and tail.state == "complete" and not tail.tool_calls
            )
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
        await check_owner(deps)
        layer_configs = {name: entry["config"] for name, entry in data.layers.items()}
        async with (
            layer_registry.open_conversation(str(data.session_id), layer_configs) as resources,
            build_provider(provider_class, provider_config) as provider,
            build_model(
                model_class,
                data.config.model_name,
                provider,
                profile={"context_window": data.config.context_window},
            ) as model,
        ):
            deps.layer_resources = resources
            # SDK instructions alone can start a turn. When no prompt is supplied,
            # let the SDK reject empty history or read a final response without them.
            with agent.override(instructions=(), toolsets=[]) if history_only else nullcontext():
                result = await agent.run(
                    data.user_prompt,
                    model=model,
                    model_settings=cast(ModelSettings, data.config.model_settings),
                    deps=deps,
                    message_history=history,
                    conversation_id=str(data.session_id),
                    event_stream_handler=handle_deltas,
                )
        await check_owner(deps)
        async with self._session_factory() as db, db.begin():
            await SessionRepository(db).complete(
                data.session_id,
                activity_id,
                run_id,
                runner_state=result.all_messages_json().decode("utf-8"),
                layer_states=layer_registry.snapshot(deps),
            )
        return result.output

"""Worker-owned create/run/close Activities with durable conditional ownership.

Each invocation claims its pending operation before I/O. Heartbeats carry no
checkpoint. Business success commits with session state before Temporal result
acknowledgment; lost acknowledgments cannot overwrite or compensate that success.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from typing import cast
from uuid import UUID

import anyio
from pydantic import ValidationError
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.settings import ModelSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from kapy.agent_output import AgentOutputService
from kapy.control.models.runtime import build_model, build_provider, resolve_classes
from kapy.control.sessions.operations import OperationRepository
from kapy.control.sessions.types import (
    OperationKind,
    OwnershipLost,
    SessionRecord,
)
from kapy.layers.registry import layer_registry
from kapy.lifecycle import LifecycleStatus

from .agent import agent
from .output import handle_deltas
from .scopes import check_owner, close_scopes, create_scopes
from .types import RunnerDeps, RunnerInput


class RunnerActivity:
    """Borrow Worker resources; invocations own model, deps, scopes and heartbeat task."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        output_service: AgentOutputService,
    ) -> None:
        self._session_factory = session_factory
        self._output_service = output_service

    @asynccontextmanager
    async def _owned(self, activity_id: UUID, kind: OperationKind):
        info = activity.info()
        if info.activity_id != str(activity_id) or info.activity_run_id is None:
            raise OwnershipLost("Standalone Activity identity is required")
        async with self._session_factory() as db, db.begin():
            operation = await OperationRepository(db).claim(activity_id, kind, info.activity_run_id)
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            yield operation
        except BaseException:
            # Close the failed task's resources before publishing failure. A
            # committed success (including an uncertain commit) always wins.
            with anyio.CancelScope(shield=True):
                try:
                    async with self._session_factory() as db, db.begin():
                        await OperationRepository(db).fail(activity_id, "Session operation failed")
                except OwnershipLost:
                    pass
            raise
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
    async def create(self, activity_id: UUID) -> SessionRecord:
        async with self._owned(activity_id, OperationKind.CREATE) as operation:
            return await create_scopes(
                self._session_factory, activity_id, operation.session_id, operation.input
            )

    @activity.defn(name="kapy.close_session")
    async def close(self, activity_id: UUID) -> SessionRecord:
        async with self._owned(activity_id, OperationKind.CLOSE) as operation:
            return await close_scopes(
                self._session_factory, activity_id, operation.session_id, operation.input
            )

    @activity.defn(name="kapy.run_agent")
    async def run(self, activity_id: UUID) -> str:
        async with self._owned(activity_id, OperationKind.RUN) as operation:
            data = RunnerInput.model_validate(operation.input)
            return await self._run(activity_id, data)

    async def _run(self, activity_id: UUID, data: RunnerInput) -> str:
        try:
            history = (
                []
                if data.runner_state is None
                else ModelMessagesTypeAdapter.validate_json(data.runner_state)
            )
        except ValidationError as exc:
            raise UserError("Invalid runner_state message history") from exc
        try:
            layers = layer_registry.load(
                {name: entry["config"] for name, entry in data.layers.items()},
                {name: entry["state"] for name, entry in data.layers.items()},
            )
            provider_config = data.config.provider_config()
            provider_class, model_class = resolve_classes(provider_config)
        except (ValueError, TypeError) as exc:
            raise UserError("Invalid model or layer execution configuration") from exc
        deps = RunnerDeps(
            session_id=data.session_id,
            activity_id=activity_id,
            session_factory=self._session_factory,
            output_service=self._output_service,
            layers=layers,
        )
        await check_owner(self._session_factory, activity_id, (LifecycleStatus.RUNNING,))
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
        async with self._session_factory() as db, db.begin():
            await OperationRepository(db).complete(
                activity_id,
                LifecycleStatus.READY,
                result.output,
                runner_state=result.all_messages_json().decode("utf-8"),
                layer_states=layer_registry.snapshot(deps),
            )
        return result.output

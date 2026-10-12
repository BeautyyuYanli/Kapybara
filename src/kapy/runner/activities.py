"""Run the complete static Agent and final save in one standalone Activity.

The Worker owns database/output services. Each invocation owns its provider,
model, writable deps and heartbeat task. History uses short committed transactions;
failure or cancellation before the final save commits retains the previous snapshot.
Database commit and Temporal completion acknowledgment are separate: later failure
can leave a saved snapshot without a confirmed Activity success. Committed history
and remote effects remain. There is no checkpoint or internal Activity dispatch.
"""

import asyncio
from contextlib import suppress
from typing import cast

from pydantic import ValidationError
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.settings import ModelSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity

from kapy.agent_output import AgentOutputService
from kapy.control.models.runtime import build_model, build_provider, resolve_classes
from kapy.control.sessions.repository import SessionRepository
from kapy.layers.registry import layer_registry

from .agent import agent
from .output import handle_deltas
from .types import RunnerDeps, RunnerInput


class RunnerActivity:
    """Borrow process resources; concurrent calls share no model or mutable run State."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], output_service: AgentOutputService
    ) -> None:
        self._session_factory = session_factory
        self._output_service = output_service

    @activity.defn(name="kapy.run_agent")
    async def run(self, data: RunnerInput) -> str:
        """Restore the fixed input, execute once, commit messages/State and return output.

        Heartbeats permit cancellation and detect a lost Worker; they carry no
        checkpoints. The submitting client must disable Temporal retries.
        """
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            return await self._run(data)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    @staticmethod
    async def _heartbeat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(2)

    async def _run(self, data: RunnerInput) -> str:
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
            session_factory=self._session_factory,
            output_service=self._output_service,
            layers=layers,
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
        layer_states = layer_registry.snapshot(deps)
        async with self._session_factory.begin() as db:
            await SessionRepository(db).save_runner_state(
                data.session_id,
                expected_version=data.runner_state_version,
                runner_state=result.all_messages_json().decode("utf-8"),
                layer_states=layer_states,
            )
        return result.output

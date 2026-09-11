"""User-side session configuration, interaction channels and runner composition.

Ordinary methods own short transactions. Consumption borrows the runner's already
fenced transaction, preserving atomic queue-to-history transfer. This service
owns no long-lived ORM session, Agent, task, or engine. Optional output transport
is borrowed from agent_output; stream_output owns each subscription and joins
original history to live events without holding a transaction during iteration.
"""

import math
from collections.abc import AsyncGenerator, Sequence
from contextlib import nullcontext
from functools import partial
from typing import cast
from uuid import UUID

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kapy.tmpv2.agent_output import AgentOutputService
from kapy.tmpv2.agent_runner import (
    HistoryMessage,
    InputBatch,
    MessageCommitted,
    OutputEvent,
    TurnResult,
    UserInput,
)
from kapy.tmpv2.agent_runner import start_runner as run_agent_session
from kapy.tmpv2.agent_runner.compaction import require_nonnegative_int
from kapy.tmpv2.agent_runner.repository import AgentRepository
from kapy.tmpv2.control.models.repository import ModelRepository
from kapy.tmpv2.control.models.runtime import (
    build_model,
    build_provider,
    resolve_classes,
    validate_settings,
)
from kapy.tmpv2.control.types import validate_pagination

from .repository import SessionRepository
from .types import CreateSession, InputChannel, SessionInput, SessionRecord, UpdateSession


class SessionService:
    """User-side entry point; configure the same finite heartbeat policy on every worker.

    The instance stores only borrowed factories/output transport and heartbeat
    values. Per-start model resources and overrides are scoped to that async call.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        output_service: AgentOutputService | None = None,
        heartbeat_interval: float = 10.0,
        heartbeat_timeout: float = 60.0,
    ) -> None:
        if not (
            math.isfinite(heartbeat_interval)
            and math.isfinite(heartbeat_timeout)
            and 0 < heartbeat_interval < heartbeat_timeout
        ):
            raise ValueError("Require finite 0 < heartbeat_interval < heartbeat_timeout")
        self._session_factory = session_factory
        self._output_service = output_service
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout

    async def create_session(self, data: CreateSession) -> SessionRecord:
        """Save validated configuration without starting execution or allocating SDK clients."""
        async with self._session_factory.begin() as db:
            models = ModelRepository(db)
            model = await models.get_model(data.provider_id, data.model_name)
            provider = await models.get_provider_config(data.provider_id)
            _, model_cls = resolve_classes(provider)
            settings = validate_settings(model_cls, data.model_settings)
            validate_settings(model_cls, model.settings | settings)
            return await SessionRepository(db).create_session(
                data.model_copy(update={"model_settings": settings})
            )

    async def get_session(self, session_id: UUID) -> SessionRecord:
        async with self._session_factory.begin() as db:
            return await SessionRepository(db).get_session(session_id)

    async def list_sessions(
        self,
        *,
        provider_id: UUID | None = None,
        model_name: str | None = None,
        offset: int = 0,
        limit: int = 100,
    ) -> tuple[SessionRecord, ...]:
        validate_pagination(offset, limit)
        async with self._session_factory.begin() as db:
            return await SessionRepository(db).list_sessions(
                provider_id=provider_id, model_name=model_name, offset=offset, limit=limit
            )

    async def update_session(self, session_id: UUID, data: UpdateSession) -> SessionRecord:
        """Replace supplied fields; model switches supply both parts of the model identity."""
        async with self._session_factory.begin() as db:
            repo = SessionRepository(db)
            current = await repo.get_session(session_id)
            values = data.model_dump(exclude_unset=True)
            updated = current.model_copy(update=values)
            models = ModelRepository(db)
            model = await models.get_model(updated.provider_id, updated.model_name)
            provider = await models.get_provider_config(updated.provider_id)
            _, model_cls = resolve_classes(provider)
            settings = validate_settings(model_cls, updated.model_settings)
            validate_settings(model_cls, model.settings | settings)
            return await repo.update_session(session_id, values | {"model_settings": settings})

    async def is_runner_running(self, session_id: UUID) -> bool:
        """Observe a live lease using this service's timeout, including a still-owned done state."""
        async with self._session_factory.begin() as db:
            await SessionRepository(db).get_session(session_id)
            return await AgentRepository(db).is_runner_running(
                session_id, heartbeat_timeout=self._heartbeat_timeout
            )

    async def enqueue_input(
        self, session_id: UUID, channel: InputChannel, content: UserInput
    ) -> SessionInput:
        async with self._session_factory.begin() as db:
            repo = SessionRepository(db)
            await repo.get_session(session_id)
            item = await repo.enqueue_input(session_id, channel, content)
        return item

    async def read_inputs(
        self, session_id: UUID, channel: InputChannel
    ) -> tuple[SessionInput, ...]:
        """Read FIFO inputs without consumption or requiring a business session row."""
        async with self._session_factory.begin() as db:
            return await SessionRepository(db).read_inputs(session_id, channel)

    async def request_cancel(self, session_id: UUID) -> None:
        async with self._session_factory.begin() as db:
            repo = SessionRepository(db)
            await repo.get_session(session_id)
            await repo.set_cancel(session_id)

    async def read_cancel(self, session_id: UUID) -> bool:
        async with self._session_factory.begin() as db:
            repo = SessionRepository(db)
            await repo.get_session(session_id)
            return await repo.read_cancel(session_id)

    async def consume_inputs(
        self,
        session_id: UUID,
        channel: InputChannel,
        *,
        db: AsyncSession,
        ids: Sequence[int],
    ) -> None:
        """Borrow the input checkpoint transaction; never independently commit."""
        await SessionRepository(db).consume_inputs(session_id, channel, ids=ids)

    async def consume_cancel(self, session_id: UUID, *, db: AsyncSession) -> bool:
        """Borrow the runner transaction after its lock_owned check."""
        return await SessionRepository(db).consume_cancel(session_id)

    async def start_runner[DepsT, OutputT](
        self,
        session_id: UUID,
        *,
        agent: Agent[DepsT, OutputT],
        deps: DepsT = None,
        realtime_output: bool = False,
        output_flush_interval: float = 0.5,
    ) -> TurnResult[OutputT]:
        """Await steer/cancel turns and queued runs, optionally broadcasting live output.

        Disabled output needs no transport. Enabled output requires output_service
        before acquiring a lease and owns one publisher across all queued runs.
        The flush interval is finite and nonnegative; zero publishes immediately.
        Session/model/provider configuration is read once and remains fixed across
        queued runs and compaction. The Agent keeps its prompts/tools/output type;
        a task-local override supplies the stored model and merged request settings.
        Configuration errors precede output publication, lease acquisition and input
        consumption. Provider and Model contexts outlive the complete runner loop.
        """
        if realtime_output and self._output_service is None:
            raise RuntimeError("Realtime output requires an output service")

        async with self._session_factory.begin() as db:
            session = await SessionRepository(db).get_session(session_id)
            repo = ModelRepository(db)
            model = await repo.get_model(session.provider_id, session.model_name)
            config = await repo.get_provider_config(session.provider_id)
        threshold = resolve_compaction_threshold(
            session.compaction_threshold_tokens, model.context_window
        )
        provider_cls, model_cls = resolve_classes(config)
        settings = validate_settings(model_cls, model.settings | session.model_settings)

        async def read_batch(channel: InputChannel) -> InputBatch | None:
            rows = await self.read_inputs(session_id, channel)
            if not rows:
                return None
            ids = tuple(row.id for row in rows)

            async def consume(db: AsyncSession) -> None:
                await self.consume_inputs(session_id, channel, db=db, ids=ids)

            return InputBatch(tuple(row.content for row in rows), consume)

        async def consume_cancel(db: AsyncSession) -> bool:
            return await self.consume_cancel(session_id, db=db)

        publisher = (
            self._output_service.publisher(session_id, flush_interval=output_flush_interval)
            if realtime_output and self._output_service is not None
            else nullcontext(None)
        )
        async with (
            build_provider(provider_cls, config) as provider,
            build_model(
                model_cls,
                model.model_name,
                provider,
                profile={"context_window": model.context_window},
            ) as sdk_model,
        ):
            with agent.override(model=sdk_model, model_settings=cast(ModelSettings, settings)):
                async with publisher as on_output:
                    return await run_agent_session(
                        session_id,
                        agent=agent,
                        session_factory=self._session_factory,
                        deps=deps,
                        read_steer=partial(read_batch, "steer"),
                        read_queued=partial(read_batch, "queued"),
                        consume_cancel=consume_cancel,
                        heartbeat_interval=self._heartbeat_interval,
                        heartbeat_timeout=self._heartbeat_timeout,
                        compaction_threshold_tokens=threshold,
                        compaction_replay_turns=session.compaction_replay_turns,
                        on_output=on_output,
                    )

    async def stream_output(
        self, session_id: UUID, *, start_seq: int = 0
    ) -> AsyncGenerator[OutputEvent]:
        """Replay original history from inclusive start_seq, then follow live events.

        start_seq must be a nonnegative integer, otherwise ValueError is raised.
        An output_service is required even for history replay, otherwise RuntimeError
        is raised. Validation, subscription and history reads begin on iteration.
        Live continuation requires the producer to enable realtime_output.

        Subscribe before reading so overlapping commits can be deduplicated. Only
        committed messages advance the cursor; gaps trigger one short database read.
        No transaction survives a yield. Missing predecessors raise RuntimeError.
        This stream continues across runner lifetimes, with no polling or guaranteed
        delivery of a lost final notification. Reconnect from the last committed
        seq + 1 and discard provisional text. Use aclosing when stopping early.
        """
        require_nonnegative_int(start_seq, "start_seq")
        if self._output_service is None:
            raise RuntimeError("Streaming output requires an output service")
        await self.get_session(session_id)
        last_seq = start_seq - 1

        async def read_history() -> tuple[HistoryMessage, ...]:
            async with self._session_factory.begin() as db:
                return await AgentRepository(db).read_history_entries(
                    session_id, after_seq=last_seq
                )

        async with self._output_service.subscribe(session_id) as events:
            for entry in await read_history():
                if entry.seq != last_seq + 1:
                    raise RuntimeError("Session history has a sequence gap")
                last_seq = entry.seq
                yield MessageCommitted(entry)
            async for event in events:
                seq = (
                    event.message.seq if isinstance(event, MessageCommitted) else event.response_seq
                )
                if seq > last_seq + 1:
                    for entry in await read_history():
                        if entry.seq != last_seq + 1:
                            raise RuntimeError("Session history has a sequence gap")
                        last_seq = entry.seq
                        yield MessageCommitted(entry)
                if seq <= last_seq:
                    continue
                if seq != last_seq + 1:
                    raise RuntimeError("Session history is missing an event predecessor")
                if isinstance(event, MessageCommitted):
                    last_seq = seq
                yield event


def resolve_compaction_threshold(configured: int | None, context_window: int | None) -> int:
    """Resolve business defaults; None selects model capacity rather than disabling compaction."""
    if configured is not None:
        if type(configured) is not int or configured <= 0:
            raise ValueError("compaction_threshold_tokens must be a positive integer")
        return configured
    if context_window is None:
        raise ValueError(
            "session.compaction_threshold_tokens is required when context_window is unknown"
        )
    return max(1, context_window * 7 // 10)

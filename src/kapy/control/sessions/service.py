"""User-side session configuration, interaction channels and runner composition.

Ordinary methods own short transactions. Consumption borrows the runner's already
fenced transaction, preserving atomic queue-to-history transfer. This service
owns no long-lived ORM session, Agent, or engine. Optional output transport
is borrowed from agent_output; each live call owns its subscription/read task and joins
original history to live events without holding a transaction during iteration.
"""

import asyncio
import math
from collections.abc import AsyncGenerator, Callable, Sequence
from contextlib import nullcontext
from functools import partial
from typing import Any, cast
from uuid import UUID

from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kapy.agent_output import AgentOutputService
from kapy.agent_runner import (
    ContextPolicy,
    HistoryMessage,
    InputBatch,
    MessageCommitted,
    OutputEvent,
    SessionBusy,
    TurnResult,
    UserInput,
    summary_context_policy,
)
from kapy.agent_runner import start_runner as run_agent_session
from kapy.agent_runner.repository import AgentRepository
from kapy.control.models.repository import ModelRepository
from kapy.control.models.runtime import (
    build_model,
    build_provider,
    resolve_classes,
    validate_settings,
)
from kapy.control.models.types import ModelRecord
from kapy.pagination import BeforeSeqPagination, Page, validate_pagination
from kapy.session_lease import is_session_busy

from .repository import SessionRepository
from .types import (
    CreateSession,
    InputChannel,
    InputSubmission,
    SessionInput,
    SessionRecord,
    SubmitInput,
    UpdateSession,
)

type ContextPolicyFactory = Callable[[SessionRecord, ModelRecord, Agent[Any, Any]], ContextPolicy]


class SessionService:
    """User-side entry point; configure the same finite heartbeat policy on every worker.

    The instance stores only borrowed factories/output transport and heartbeat
    and live polling values, plus an optional pure context-policy factory. Per-start
    model resources, frozen policy and overrides are scoped to that async call.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        output_service: AgentOutputService | None = None,
        heartbeat_interval: float = 10.0,
        heartbeat_timeout: float = 60.0,
        live_poll_interval: float = 5.0,
        context_policy_factory: ContextPolicyFactory | None = None,
    ) -> None:
        if not (
            math.isfinite(heartbeat_interval)
            and math.isfinite(heartbeat_timeout)
            and 0 < heartbeat_interval < heartbeat_timeout
        ):
            raise ValueError("Require finite 0 < heartbeat_interval < heartbeat_timeout")
        if not math.isfinite(live_poll_interval) or live_poll_interval <= 0:
            raise ValueError("live_poll_interval must be finite and positive")
        self._live_poll_interval = live_poll_interval
        self._session_factory = session_factory
        self._output_service = output_service
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout
        self._context_policy_factory = context_policy_factory

    async def create_session(self, data: CreateSession) -> SessionRecord:
        """Save configuration without execution or SDK clients.

        An omitted/None threshold stays None for a known model capacity; otherwise
        creation stores 70% of 256 Ki tokens, rounded down. Updates do not default it.
        """
        async with self._session_factory.begin() as db:
            models = ModelRepository(db)
            model = await models.get_model(data.provider_id, data.model_name)
            provider = await models.get_provider_config(data.provider_id)
            _, model_cls = resolve_classes(provider)
            settings = validate_settings(model_cls, data.model_settings)
            validate_settings(model_cls, model.settings | settings)
            threshold = data.compaction_threshold_tokens
            if threshold is None and model.context_window is None:
                threshold = 256 * 1024 * 7 // 10
            return await SessionRepository(db).create_session(
                data.model_copy(
                    update={
                        "model_settings": settings,
                        "compaction_threshold_tokens": threshold,
                    }
                )
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
    ) -> Page[SessionRecord]:
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
        """Observe lease occupancy with this service's timeout, not Agent generation.

        The owner can be any cooperating session operation, even at a done checkpoint.
        This compatibility name neither acquires ownership nor proves process liveness.
        """
        async with self._session_factory.begin() as db:
            return await is_session_busy(db, session_id, heartbeat_timeout=self._heartbeat_timeout)

    async def enqueue_input(
        self, session_id: UUID, channel: InputChannel, content: UserInput
    ) -> SessionInput:
        async with self._session_factory.begin() as db:
            repo = SessionRepository(db)
            await repo.get_session(session_id)
            item = await repo.enqueue_input(session_id, channel, content)
        return item

    async def submit_input(self, session_id: UUID, data: SubmitInput) -> InputSubmission:
        """Commit input before observing the lease; callers choose how to schedule execution."""
        item = await self.enqueue_input(session_id, data.channel, data.content)
        running = await self.is_runner_running(session_id)
        return InputSubmission(input=item, should_start_runner=not running)

    async def delete_input(self, session_id: UUID, input_id: int) -> bool:
        """Withdraw a pending input; False means it was consumed, deleted or never present."""
        if type(input_id) is not int or input_id <= 0:
            raise ValueError("input_id must be a positive integer")
        async with self._session_factory.begin() as db:
            return await SessionRepository(db).delete_input(session_id, input_id)

    async def read_history(
        self, session_id: UUID, *, before_seq: int | None = None, limit: int = 100
    ) -> Page[HistoryMessage]:
        """Read the latest page before an exclusive cursor, ordered oldest to newest."""
        BeforeSeqPagination(before_seq=before_seq, limit=limit)
        async with self._session_factory.begin() as db:
            return await AgentRepository(db).read_history_page(
                session_id, before_seq=before_seq, limit=limit
            )

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
            return await SessionRepository(db).read_cancel(session_id)

    async def consume_inputs(
        self,
        session_id: UUID,
        channel: InputChannel,
        *,
        db: AsyncSession,
        ids: Sequence[int],
    ) -> tuple[SessionInput, ...]:
        """Return actual consumption in the borrowed checkpoint transaction; never commit."""
        return await SessionRepository(db).consume_inputs(session_id, channel, ids=ids)

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
        The flush interval is finite and nonnegative; zero schedules background
        publication immediately.
        Session/model/provider configuration is read once and remains fixed across
        queued runs and page actions. The Agent keeps its prompts/tools/output type;
        a task-local override supplies the stored model and merged request settings.
        Configuration errors precede output publication, lease acquisition and input
        consumption. Provider and Model contexts outlive the complete runner loop.
        After lease release, pending inputs trigger reacquisition with the same
        configuration. Across the entire call, output retains the last non-None
        output, while finished comes from the last normally returned result. Later
        empty runs or fully withdrawn inputs do not clear an earlier output.
        A later SessionBusy returns the preceding successful aggregate; an initial
        SessionBusy and all other failures propagate. No background task is created here.
        """
        if realtime_output and self._output_service is None:
            raise RuntimeError("Realtime output requires an output service")

        async with self._session_factory.begin() as db:
            session = await SessionRepository(db).get_session(session_id)
            repo = ModelRepository(db)
            model = await repo.get_model(session.provider_id, session.model_name)
            config = await repo.get_provider_config(session.provider_id)
        policy = (
            self._context_policy_factory(session, model, agent)
            if self._context_policy_factory is not None
            else summary_context_policy(
                agent,
                deps=deps,
                threshold_tokens=resolve_compaction_threshold(
                    session.compaction_threshold_tokens, model.context_window
                ),
                replay_turns=session.compaction_replay_turns,
            )
        )
        provider_cls, model_cls = resolve_classes(config)
        settings = validate_settings(model_cls, model.settings | session.model_settings)

        async def read_batch(channel: InputChannel) -> InputBatch | None:
            rows = await self.read_inputs(session_id, channel)
            if not rows:
                return None
            ids = tuple(row.id for row in rows)

            async def consume(db: AsyncSession) -> tuple[UserInput, ...]:
                accepted = await self.consume_inputs(session_id, channel, db=db, ids=ids)
                return tuple(row.content for row in accepted)

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
                    result: TurnResult[OutputT] | None = None
                    while True:
                        try:
                            current = await run_agent_session(
                                session_id,
                                agent=agent,
                                session_factory=self._session_factory,
                                deps=deps,
                                read_steer=partial(read_batch, "steer"),
                                read_queued=partial(read_batch, "queued"),
                                consume_cancel=consume_cancel,
                                heartbeat_interval=self._heartbeat_interval,
                                heartbeat_timeout=self._heartbeat_timeout,
                                context_policy=policy,
                                on_output=on_output,
                            )
                        except SessionBusy:
                            if result is None:
                                raise
                            return result
                        result = TurnResult(
                            current.finished,
                            current.output
                            if current.output is not None
                            else (result.output if result is not None else None),
                        )
                        # run_agent_session returns only after its lease release commits.
                        if not (
                            await self.read_inputs(session_id, "queued")
                            or await self.read_inputs(session_id, "steer")
                        ):
                            return result

    async def live(
        self, session_id: UUID, *, after_seq: int = -1
    ) -> AsyncGenerator[list[OutputEvent]]:
        """Yield nonempty batches of original history and live output after the applied seq.

        after_seq must be an integer >= -1, otherwise ValueError is raised.
        An output_service is required even for history replay, otherwise RuntimeError
        is raised. Validation, subscription and history reads begin on iteration.
        Realtime output supplies provisional deltas; complete messages are also polled.

        Subscribe before reading to deduplicate overlapping commits. Only complete
        messages advance the cursor. Gaps trigger a short read; unresolved gaps wait
        for later events or the next poll without skipping predecessors. Polling uses
        live_poll_interval, independent of incoming traffic. No transaction spans a
        yield. Consumer backpressure pauses polling but not transport reception.
        Database/subscription errors end the stream; runner completion does not.
        Use aclosing when stopping early.
        """
        if type(after_seq) is not int or after_seq < -1:
            raise ValueError("after_seq must be an integer >= -1")
        if self._output_service is None:
            raise RuntimeError("Streaming output requires an output service")
        last_seq = after_seq

        async def read_history() -> tuple[HistoryMessage, ...]:
            async with self._session_factory.begin() as db:
                return await AgentRepository(db).read_history_entries(
                    session_id, after_seq=last_seq
                )

        loop = asyncio.get_running_loop()
        next_poll_at = loop.time()
        pending = None
        async with self._output_service.subscribe(session_id) as events:
            try:
                while True:
                    if loop.time() >= next_poll_at:
                        entries = await read_history()
                        next_poll_at = loop.time() + self._live_poll_interval
                        replay: list[OutputEvent] = []
                        for entry in entries:
                            if entry.seq != last_seq + 1:
                                break
                            last_seq = entry.seq
                            replay.append(MessageCommitted(entry))
                        if replay:
                            yield replay

                    if pending is None:
                        pending = asyncio.ensure_future(anext(events))
                    done, _ = await asyncio.wait(
                        {pending}, timeout=max(0, next_poll_at - loop.time())
                    )
                    # Preserve the same read across polling timeouts, including its
                    # result when both the event and the deadline become ready.
                    if not done or loop.time() >= next_poll_at:
                        continue
                    try:
                        batch = pending.result()
                    except StopAsyncIteration:
                        return
                    pending = None
                    result: list[OutputEvent] = []
                    for event in batch:
                        seq = (
                            event.message.seq
                            if isinstance(event, MessageCommitted)
                            else event.response_seq
                        )
                        if seq > last_seq + 1:
                            entries = await read_history()
                            next_poll_at = loop.time() + self._live_poll_interval
                            for entry in entries:
                                if entry.seq != last_seq + 1:
                                    break
                                last_seq = entry.seq
                                result.append(MessageCommitted(entry))
                        if seq != last_seq + 1:
                            continue
                        if isinstance(event, MessageCommitted):
                            last_seq = seq
                        result.append(event)
                    # Commits later in this batch (including backfill) supersede
                    # previews that have not yet reached the consumer.
                    result = [
                        item
                        for item in result
                        if isinstance(item, MessageCommitted) or item.response_seq > last_seq
                    ]
                    if result:
                        yield result
            finally:
                # Finish anext before subscribe closes its underlying iterator.
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)


def resolve_compaction_threshold(configured: int | None, context_window: int | None) -> int:
    """Resolve a stored threshold at startup; None requires a known model capacity."""
    if configured is not None:
        if type(configured) is not int or configured <= 0:
            raise ValueError("compaction_threshold_tokens must be a positive integer")
        return configured
    if context_window is None:
        raise ValueError(
            "session.compaction_threshold_tokens is required when context_window is unknown"
        )
    return max(1, context_window * 7 // 10)

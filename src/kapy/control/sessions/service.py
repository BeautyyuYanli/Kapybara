"""User-side session configuration, interaction channels and runner composition.

Ordinary methods own short transactions. Consumption borrows the runner's already
fenced transaction, preserving atomic queue-to-history transfer. This service
owns no long-lived ORM session, Agent, or engine. Optional output transport
is borrowed from agent_output; each live call owns its subscription/read task and joins
original history to live events without holding a transaction during iteration.
"""

import asyncio
import math
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, nullcontext
from dataclasses import dataclass
from functools import partial
from typing import Any, cast
from uuid import UUID

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.providers import Provider
from pydantic_ai.settings import ModelSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kapy.agent_output import AgentOutputService
from kapy.agent_plugins import AgentPluginService, PluginRegistry
from kapy.agent_plugins.repository import BindingRepository
from kapy.agent_runner import (
    HistoryMessage,
    InputBatch,
    MessageCommitted,
    OutputEvent,
    RunnerExecution,
    SessionBusy,
    TurnResult,
    UserInput,
)
from kapy.agent_runner import start_runner as run_agent_session
from kapy.agent_runner.repository import AgentRepository
from kapy.context_plugins import ContextPluginRegistry, create_default_registry
from kapy.control.models.repository import ModelRepository
from kapy.control.models.runtime import (
    build_model,
    build_provider,
    resolve_classes,
    validate_settings,
)
from kapy.control.models.types import ModelRecord, ProviderConfig
from kapy.control.types import utc_now
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.pagination import BeforeSeqPagination, Page, validate_pagination
from kapy.session_lease import SessionLease, is_session_busy, open_session_lease

from .repository import SessionRepository, lock_session
from .types import (
    CreateSession,
    InputChannel,
    InputSubmission,
    SessionInput,
    SessionRecord,
    SubmitInput,
    UpdateSession,
)


@dataclass(frozen=True)
class SessionExecutionConfig:
    """Validated configuration snapshot reused across one start call's leases.

    Contains values only, without open model/plugin resources. Each execution
    factory uses this snapshot to own a fresh resource scope inside its lease.
    """

    session: SessionRecord
    model: ModelRecord
    provider: ProviderConfig
    provider_class: type[Provider]
    model_class: type[Model]
    model_settings: ModelSettings
    compaction_threshold_tokens: int


type SessionExecutionFactory = Callable[
    [SessionService, SessionExecutionConfig, ContextPluginRegistry, SessionLease],
    AbstractAsyncContextManager[RunnerExecution[Any]],
]


class SessionService:
    """User-side entry point; configure the same finite heartbeat policy on every worker.

    The instance stores only borrowed factories/output transport and heartbeat
    and live polling values, plus plugin/execution factories and a context plugin registry. Model
    configuration is fixed for each start call. The execution factory owns model
    and plugin resources inside each lease. Direct caller-owned Agents use a
    separate adapter with a task-local model/settings override.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        output_service: AgentOutputService | None = None,
        heartbeat_interval: float = 10.0,
        heartbeat_timeout: float = 60.0,
        takeover_grace_period: float = 30.0,
        live_poll_interval: float = 5.0,
        context_plugin_registry: ContextPluginRegistry | None = None,
        plugin_service: AgentPluginService | None = None,
        execution_factory: SessionExecutionFactory | None = None,
    ) -> None:
        if not (
            math.isfinite(heartbeat_interval)
            and math.isfinite(heartbeat_timeout)
            and 0 < heartbeat_interval < heartbeat_timeout
        ):
            raise ValueError("Require finite 0 < heartbeat_interval < heartbeat_timeout")
        if not math.isfinite(takeover_grace_period) or takeover_grace_period <= 0:
            raise ValueError("takeover_grace_period must be finite and positive")
        if not math.isfinite(live_poll_interval) or live_poll_interval <= 0:
            raise ValueError("live_poll_interval must be finite and positive")
        self._live_poll_interval = live_poll_interval
        self._session_factory = session_factory
        self._output_service = output_service
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout
        self._takeover_grace_period = takeover_grace_period
        self._context_plugin_registry = context_plugin_registry or create_default_registry()
        self.plugins = plugin_service or AgentPluginService(session_factory, PluginRegistry())
        self._execution_factory = execution_factory

    async def create_session(self, data: CreateSession) -> SessionRecord:
        """Save configuration without execution or SDK clients.

        An omitted/None threshold stays None for a known model capacity; otherwise
        creation stores 70% of 256 Ki tokens, rounded down. Updates do not default it.
        """
        prepared = self.plugins.prepare_bindings(data.plugins)
        async with self._session_factory.begin() as db:
            try:
                model = await ModelRepository(db).get_model(data.provider_id, data.model_name)
            except LookupError:
                model = None
            threshold = data.compaction_threshold_tokens
            if threshold is None and (model is None or model.context_window is None):
                threshold = 256 * 1024 * 7 // 10
            record = await SessionRepository(db).create_session(
                data.model_copy(update={"compaction_threshold_tokens": threshold})
            )
            self.plugins.create_bindings(db, record.id, prepared)
        return record

    async def require_ready(self, session_id: UUID, *, db: AsyncSession | None = None) -> None:
        """Lock the session for intake or lease admission in the caller's transaction."""
        if db is None:
            async with self._session_factory.begin() as owned:
                await self.require_ready(session_id, db=owned)
            return
        row = await lock_session(db, session_id)
        if row.status != LifecycleStatus.READY:
            raise LifecycleError(f"Session {session_id} is {row.status}")

    async def close_session(self, session_id: UUID) -> SessionRecord:
        """Acquire exclusive ownership before deciding closing; Busy changes no state.

        Close never changes cancellation or pending inputs. Failures retain the
        irreversible closing decision and completed bindings for explicit retry.
        Registered resources are cleaned under the lease, including plugin exit;
        fencing cannot revoke already-issued external requests from a lost owner.
        """
        async with open_session_lease(
            session_id,
            session_factory=self._session_factory,
            heartbeat_interval=self._heartbeat_interval,
            heartbeat_timeout=self._heartbeat_timeout,
            takeover_grace_period=self._takeover_grace_period,
        ) as lease:
            async with self._session_factory.begin() as db:
                await lease.lock_owned(db)
                row = await lock_session(db, session_id)
                if row.status == LifecycleStatus.CLOSED:
                    return SessionRecord.model_validate(row)
                row.status, row.updated_at = LifecycleStatus.CLOSING, utc_now()
                bindings = await BindingRepository(db).list(session_id)
            for binding in bindings:
                if binding.status != LifecycleStatus.CLOSED:
                    await self.plugins.close_binding(binding, lease=lease)
            async with self._session_factory.begin() as db:
                await lease.lock_owned(db)
                row = await lock_session(db, session_id)
                row.status, row.updated_at = LifecycleStatus.CLOSED, utc_now()
                return SessionRecord.model_validate(row)

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
        values = data.model_dump(exclude_unset=True)
        if data.context_plugin is not None:
            # A supplied plugin spec replaces its config; nested defaults must not
            # disappear under the PATCH model's recursive exclude_unset behavior.
            values["context_plugin"] = data.context_plugin.model_dump()
        async with self._session_factory.begin() as db:
            return await SessionRepository(db).update_session(session_id, values)

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
            await self.require_ready(session_id, db=db)
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
        agent: Agent[DepsT, OutputT] | None = None,
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
        queued runs and page actions. Each lease enters the configured execution factory,
        which owns all business assembly and execution resources. Direct Agents are
        supported for sessions without plugins through a separate adapter retaining
        prompts/tools/output type and overriding only the stored model and settings.
        Configuration reads, class resolution, settings validation and threshold
        resolution precede output publication and lease acquisition. Provider/Model
        resource construction and plugin assembly happen inside the lease; failures
        still precede input consumption.
        Provider and Model contexts enclose each leased execution, including page actions.
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
        if agent is None and self._execution_factory is None:
            raise ValueError("Provide an Agent or configure an execution factory")
        if agent is not None and await self.plugins.list_bindings(session_id):
            raise ValueError("Plugin sessions require the application execution factory")
        threshold = resolve_compaction_threshold(
            session.compaction_threshold_tokens, model.context_window
        )
        provider_cls, model_cls = resolve_classes(config)
        settings = validate_settings(model_cls, model.settings | session.model_settings)
        execution_config = SessionExecutionConfig(
            session=session,
            model=model,
            provider=config,
            provider_class=provider_cls,
            model_class=model_cls,
            model_settings=cast(ModelSettings, settings),
            compaction_threshold_tokens=threshold,
        )
        if agent is None:
            assert self._execution_factory is not None
            execution_factory = partial(
                self._execution_factory, self, execution_config, self._context_plugin_registry
            )
        else:
            execution_factory = partial(self._open_agent_execution, execution_config, agent, deps)

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
        async with publisher as on_output:
            result: TurnResult[OutputT] | None = None
            while True:
                try:
                    current = await run_agent_session(
                        session_id,
                        execution_factory=execution_factory,
                        session_factory=self._session_factory,
                        read_steer=partial(read_batch, "steer"),
                        read_queued=partial(read_batch, "queued"),
                        consume_cancel=consume_cancel,
                        heartbeat_interval=self._heartbeat_interval,
                        heartbeat_timeout=self._heartbeat_timeout,
                        takeover_grace_period=self._takeover_grace_period,
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
                # Reacquisition creates a fresh execution after lease release.
                await self.require_ready(session_id)
                if not (
                    await self.read_inputs(session_id, "queued")
                    or await self.read_inputs(session_id, "steer")
                ):
                    return result

    @asynccontextmanager
    async def _open_agent_execution[DepsT, OutputT](
        self,
        config: SessionExecutionConfig,
        agent: Agent[DepsT, OutputT],
        deps: DepsT,
        lease: SessionLease,
    ) -> AsyncIterator[RunnerExecution[OutputT]]:
        """Adapt a borrowed custom Agent without changing its prompts/tools/output."""
        session = config.session
        async with self._session_factory.begin() as db:
            await lease.lock_owned(db)
            await self.require_ready(session.id, db=db)
        async with (
            build_provider(config.provider_class, config.provider) as provider,
            build_model(
                config.model_class,
                config.model.model_name,
                provider,
                profile={"context_window": config.model.context_window},
            ) as model,
        ):
            plugin = self._context_plugin_registry.create(session.context_plugin)
            with agent.override(model=model, model_settings=config.model_settings):
                yield RunnerExecution(
                    agent=agent,
                    deps=deps,
                    context_plugin=plugin,
                    compaction_threshold_tokens=config.compaction_threshold_tokens,
                    compaction_replay_turns=session.compaction_replay_turns,
                )

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

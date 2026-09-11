"""User-side interaction channels and complete runner composition.

Ordinary methods own short transactions. Consumption borrows the runner's already
fenced transaction, preserving atomic queue-to-history transfer. This service
owns no long-lived ORM session, Agent, task, or engine. Optional output transport
is borrowed from agent_output; stream_output owns each subscription and joins
original history to live events without holding a transaction during iteration.
"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import nullcontext
from functools import partial
from uuid import UUID

from pydantic_ai import Agent
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

from .repository import SessionRepository
from .types import InputChannel, SessionInput


class SessionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        output_service: AgentOutputService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._output_service = output_service

    async def enqueue_input(
        self, session_id: UUID, channel: InputChannel, content: UserInput
    ) -> SessionInput:
        async with self._session_factory.begin() as db:
            item = await SessionRepository(db).enqueue_input(session_id, channel, content)
        return item

    async def read_inputs(
        self, session_id: UUID, channel: InputChannel
    ) -> tuple[SessionInput, ...]:
        async with self._session_factory.begin() as db:
            return await SessionRepository(db).read_inputs(session_id, channel)

    async def request_cancel(self, session_id: UUID) -> None:
        async with self._session_factory.begin() as db:
            await SessionRepository(db).set_cancel(session_id)

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
        heartbeat_interval: float = 10.0,
        heartbeat_timeout: float = 60.0,
        compaction_threshold_tokens: int | None = None,
        compaction_replay_turns: int = 10,
        realtime_output: bool = False,
        output_flush_interval: float = 0.5,
    ) -> TurnResult[OutputT]:
        """Await steer/cancel turns and queued runs, optionally broadcasting live output.

        Disabled output needs no transport. Enabled output requires output_service
        before acquiring a lease and owns one publisher across all queued runs.
        The flush interval is finite and nonnegative; zero publishes immediately.
        """
        if realtime_output and self._output_service is None:
            raise RuntimeError("Realtime output requires an output service")

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
        async with publisher as on_output:
            return await run_agent_session(
                session_id,
                agent=agent,
                session_factory=self._session_factory,
                deps=deps,
                read_steer=partial(read_batch, "steer"),
                read_queued=partial(read_batch, "queued"),
                consume_cancel=consume_cancel,
                heartbeat_interval=heartbeat_interval,
                heartbeat_timeout=heartbeat_timeout,
                compaction_threshold_tokens=compaction_threshold_tokens,
                compaction_replay_turns=compaction_replay_turns,
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

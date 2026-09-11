"""User-side interaction channels and complete runner composition.

Ordinary methods own short transactions. Consumption borrows the runner's already
fenced transaction, preserving atomic queue-to-history transfer. This service
owns no long-lived ORM session, Agent, task, or engine.
"""

from collections.abc import Sequence
from functools import partial
from uuid import UUID

from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kapy.tmpv2.agent_runner import InputBatch, TurnResult, UserInput
from kapy.tmpv2.agent_runner import start_runner as run_agent_session

from .repository import SessionRepository
from .types import InputChannel, SessionInput


class SessionService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

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
    ) -> TurnResult[OutputT]:
        """Await execution through steer/cancel turns and subsequent queued runs."""

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
        )

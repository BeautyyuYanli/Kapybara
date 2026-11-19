"""Session configuration, native Temporal execution and immutable output replay.

Creation and closure are local database operations. Executing Workers compete for
the current writer; Temporal retains run inputs, scheduling, failures and results.
Each live call owns its subscription/read task.
"""

import asyncio
import math
from collections.abc import AsyncGenerator
from datetime import timedelta
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import ActivityExecutionDescription, ActivityHandle, Client
from temporalio.common import ActivityIDConflictPolicy, ActivityIDReusePolicy, RetryPolicy
from temporalio.exceptions import ActivityAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from kapy.agent_history import HistoryMessage, HistoryRepository
from kapy.agent_output import AgentOutputService, MessageCommitted, OutputEvent, TextDelta
from kapy.layers.registry import layer_registry
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.pagination import BeforeSeqPagination, Page, validate_pagination

from .models import SessionRow
from .repository import SessionRepository
from .temporal import RUN, TEMPORAL_RPC_TIMEOUT, ensure_activity_terminal, failure_category
from .types import (
    ActivityIdentityConflict,
    CreateSession,
    RunAgentActivity,
    SessionActivity,
    SessionBusy,
    SessionRecord,
    UpdateSession,
)

_ACTIVITY_TIMEOUT = timedelta(hours=1)


class SessionService:
    """Borrow database/output/Temporal resources; keep no process-local ownership."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        output_service: AgentOutputService | None = None,
        temporal_client: Client | None = None,
        temporal_task_queue: str | None = None,
        live_poll_interval: float = 5.0,
    ) -> None:
        if not math.isfinite(live_poll_interval) or live_poll_interval <= 0:
            raise ValueError("live_poll_interval must be finite and positive")
        self._session_factory = session_factory
        self._output_service = output_service
        self._temporal_client = temporal_client
        self._temporal_task_queue = temporal_task_queue
        self._live_poll_interval = live_poll_interval

    def _client(self) -> Client:
        if self._temporal_client is None:
            raise RuntimeError("Activity calls require a Temporal client")
        return self._temporal_client

    async def _describe(
        self,
        session_id: UUID,
        activity_id: UUID,
        run_id: str | None,
        *,
        include_outcome: bool = True,
    ) -> ActivityExecutionDescription:
        description = (
            await self._client()
            .get_activity_handle(
                str(activity_id),
                run_id=run_id,
            )
            .describe(
                include_input=True,
                include_outcome=include_outcome,
                rpc_timeout=TEMPORAL_RPC_TIMEOUT,
            )
        )
        if description.activity_type != RUN:
            raise ActivityIdentityConflict(session_id, activity_id, description.activity_run_id)
        inputs = await description.input([RunAgentActivity])
        if (
            not inputs
            or len(inputs) != 1
            or inputs[0].session_id != session_id
            or not description.activity_run_id
        ):
            raise ActivityIdentityConflict(session_id, activity_id, description.activity_run_id)
        return description

    async def create_session(self, data: CreateSession) -> SessionRecord:
        """Validate fixed layer Config and insert an idle session with its default State."""
        layers = layer_registry.load(data.layers, {})
        row = SessionRow(
            **data.model_dump(exclude={"id", "layers"}),
            id=data.id or uuid4(),
            layers={name: entry["config"] for name, entry in layers.items()},
            layer_states={name: entry["state"] for name, entry in layers.items()},
        )
        async with self._session_factory.begin() as db:
            await SessionRepository(db).create(row)
        return SessionRecord.model_validate(row)

    async def start_runner(
        self,
        session_id: UUID,
        *,
        activity_id: UUID,
        user_prompt: str | None = None,
    ) -> ActivityHandle[str]:
        """Submit without reserving a writer. The Worker checks admission again."""
        if user_prompt is not None and not isinstance(user_prompt, str):
            raise ValueError("user_prompt must be a string or None")
        async with self._session_factory.begin() as db:
            if await SessionRepository(db).get_status(session_id) == LifecycleStatus.CLOSED:
                raise LifecycleError(f"Session {session_id} is closed")

        client = self._client()
        if not self._temporal_task_queue:
            raise RuntimeError("Activity submission requires a task queue")
        try:
            handle = await client.start_activity(
                RUN,
                RunAgentActivity(session_id=session_id, user_prompt=user_prompt),
                result_type=str,
                id=str(activity_id),
                task_queue=self._temporal_task_queue,
                id_conflict_policy=ActivityIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=ActivityIDReusePolicy.REJECT_DUPLICATE,
                schedule_to_close_timeout=_ACTIVITY_TIMEOUT,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                heartbeat_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
                rpc_timeout=TEMPORAL_RPC_TIMEOUT,
            )
        except ActivityAlreadyStartedError as error:
            # Retained terminal rejection supplies the precise execution to reuse.
            run_id = error.run_id
        else:
            run_id = handle.run_id
        description = await self._describe(session_id, activity_id, run_id, include_outcome=False)
        return client.get_activity_handle(
            str(activity_id),
            run_id=description.activity_run_id,
            result_type=str,
        )

    async def close_session(self, session_id: UUID) -> SessionRecord:
        """Keep a tombstone, fencing on the exact writer if Temporal confirmed it ended."""
        async with self._session_factory.begin() as db:
            repo = SessionRepository(db)
            closed = await repo.close_session(session_id)
            if closed is not None:
                return closed
            session = await repo.get_session(session_id)
        if session.status == LifecycleStatus.CLOSED:
            return session

        previous = None
        if session.current_activity_id is not None:
            assert session.current_activity_run_id is not None
            previous = (session.current_activity_id, session.current_activity_run_id)
            await ensure_activity_terminal(self._client(), *previous)

        async with self._session_factory.begin() as db:
            repo = SessionRepository(db)
            closed = await repo.close_session(session_id, previous=previous)
            if closed is not None:
                return closed
            session = await repo.get_session(session_id)
            if (
                previous is not None
                and session.status == LifecycleStatus.READY
                and session.current_activity_id is None
            ):
                # The old writer released itself after we observed its identity.
                session = await repo.close_session(session_id) or await repo.get_session(session_id)
            if session.status == LifecycleStatus.CLOSED:
                return session
        raise SessionBusy(f"Session {session_id} is occupied")

    async def get_activity(
        self,
        session_id: UUID,
        activity_id: UUID,
        *,
        run_id: str | None = None,
    ) -> SessionActivity:
        """Read a retained run's status and outcome directly from Temporal."""
        try:
            description = await self._describe(session_id, activity_id, run_id)
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                raise LookupError("Activity not found; its outcome cannot be confirmed") from None
            raise
        result = await description.result(str)
        failure = await description.outcome_failure()
        assert description.activity_run_id is not None
        return SessionActivity(
            session_id=session_id,
            activity_id=activity_id,
            run_id=description.activity_run_id,
            activity_type=RUN,
            status=description.status.name,
            run_state=description.run_state.name if description.run_state is not None else None,
            result=result,
            error=failure_category(failure) if failure is not None else None,
        )

    async def get_session(self, session_id: UUID) -> SessionRecord:
        async with self._session_factory() as db, db.begin():
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
        async with self._session_factory() as db, db.begin():
            return await SessionRepository(db).list_sessions(
                provider_id=provider_id, model_name=model_name, offset=offset, limit=limit
            )

    async def update_session(self, session_id: UUID, data: UpdateSession) -> SessionRecord:
        async with self._session_factory() as db, db.begin():
            return await SessionRepository(db).update_session(
                session_id, data.model_dump(exclude_unset=True)
            )

    async def read_history(
        self, session_id: UUID, *, before_seq: int | None = None, limit: int = 100
    ) -> Page[HistoryMessage]:
        """Read the latest page before an exclusive cursor, ordered oldest to newest."""
        BeforeSeqPagination(before_seq=before_seq, limit=limit)
        async with self._session_factory() as db, db.begin():
            return await HistoryRepository(db).read_history_page(
                session_id, before_seq=before_seq, limit=limit
            )

    async def live(
        self, session_id: UUID, *, after_seq: int = -1
    ) -> AsyncGenerator[list[OutputEvent]]:
        """Replay immutable history and follow output after an applied message.

        after_seq is an integer >= -1, acknowledged by the caller after applying
        complete messages. The connection cursor advances when complete messages
        enter its ordered output; it is not a client acknowledgement. Producers
        commit before publishing and never change old positions or fill their gaps.
        Consecutive broadcasts advance directly; a numeric jump triggers backfill.
        Subscribe before replay and periodically read history for missed commits.
        Previews never advance either cursor or trigger reads. Stored gaps are valid.
        Consumer backpressure pauses polling, not transport reception. No transaction
        spans a yield. Errors end the stream; completion does not. Use aclosing when
        stopping early; clear temporary previews and resume from the applied position.
        """
        if type(after_seq) is not int or after_seq < -1:
            raise ValueError("after_seq must be an integer >= -1")
        if self._output_service is None:
            raise RuntimeError("Streaming output requires an output service")
        last_seq = after_seq

        async def read_history() -> tuple[HistoryMessage, ...]:
            async with self._session_factory() as db, db.begin():
                return await HistoryRepository(db).read_history_entries(
                    session_id, after_seq=last_seq
                )

        def replay(entries: tuple[HistoryMessage, ...]) -> list[MessageCommitted]:
            nonlocal last_seq
            if entries:
                last_seq = entries[-1].seq
            return [MessageCommitted(entry) for entry in entries]

        loop = asyncio.get_running_loop()
        next_poll_at = loop.time()
        pending = None
        async with self._output_service.subscribe(session_id) as events:
            try:
                while True:
                    if loop.time() >= next_poll_at:
                        entries = await read_history()
                        next_poll_at = loop.time() + self._live_poll_interval
                        if entries:
                            yield list(replay(entries))

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
                    complete = {
                        event.message.seq: event
                        for event in batch
                        if isinstance(event, MessageCommitted) and event.message.seq > last_seq
                    }
                    for seq in sorted(complete):
                        if seq == last_seq + 1:
                            result.append(complete[seq])
                            last_seq = seq
                            continue
                        entries = await read_history()
                        next_poll_at = loop.time() + self._live_poll_interval
                        # Complete messages are immutable. Keep full SDK usage
                        # from the broadcast instead of normalized database fields.
                        entries = tuple(
                            complete[entry.seq].message if entry.seq in complete else entry
                            for entry in entries
                        )
                        result.extend(replay(entries))
                        break

                    # Complete messages suppress covered previews even if those
                    # arrived first. Remaining temporary events keep their order.
                    for event in batch:
                        if isinstance(event, TextDelta) and event.response_seq > last_seq:
                            result.append(event)
                    if result:
                        yield result
            finally:
                # Finish anext before subscribe closes its underlying iterator.
                if pending is not None:
                    pending.cancel()
                    await asyncio.gather(pending, return_exceptions=True)

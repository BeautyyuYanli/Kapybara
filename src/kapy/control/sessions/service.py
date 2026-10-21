"""Session configuration, native Temporal execution and immutable output replay.

Submission never reserves business state. Workers compete for the current writer
when they execute; Temporal retains inputs, scheduling, failures and results.
Queries do not mutate sessions. Each live call owns its subscription/read task.
"""

import asyncio
import math
from collections.abc import AsyncGenerator
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import ActivityFailureError, ActivityHandle, Client
from temporalio.common import ActivityIDConflictPolicy, ActivityIDReusePolicy, RetryPolicy
from temporalio.exceptions import ActivityAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from kapy.agent_history import HistoryMessage, HistoryRepository
from kapy.agent_output import AgentOutputService, MessageCommitted, OutputEvent, TextDelta
from kapy.pagination import BeforeSeqPagination, Page, validate_pagination

from .repository import SessionRepository
from .temporal import CLOSE, CREATE, RESUME, RUN, failure_category
from .types import (
    ActivityIdentityConflict,
    CreateSession,
    CreateSessionActivity,
    RunAgentActivity,
    SessionActivity,
    SessionActivityRequest,
    SessionExecutionError,
    SessionRecord,
    UpdateSession,
)

_TEMPORAL_RPC_TIMEOUT = timedelta(seconds=10)
_ACTIVITY_TIMEOUT = timedelta(hours=1)
_INPUT_TYPES = {
    CREATE: CreateSessionActivity,
    RUN: RunAgentActivity,
    RESUME: SessionActivityRequest,
    CLOSE: SessionActivityRequest,
}


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
        expected_type: str | None = None,
        include_outcome: bool = True,
    ):
        description = (
            await self._client()
            .get_activity_handle(
                str(activity_id),
                run_id=run_id,
            )
            .describe(
                include_input=True,
                include_outcome=include_outcome,
                rpc_timeout=_TEMPORAL_RPC_TIMEOUT,
            )
        )
        input_type = _INPUT_TYPES.get(description.activity_type)
        if input_type is None or (
            expected_type is not None and description.activity_type != expected_type
        ):
            raise ActivityIdentityConflict(session_id, activity_id, description.activity_run_id)
        inputs = await description.input([input_type])
        if (
            not inputs
            or len(inputs) != 1
            or inputs[0].session_id != session_id
            or not description.activity_run_id
        ):
            raise ActivityIdentityConflict(session_id, activity_id, description.activity_run_id)
        return description

    async def _start[T](
        self, name: str, request: SessionActivityRequest, activity_id: UUID, result_type: type[T]
    ) -> ActivityHandle[T]:
        client = self._client()
        if not self._temporal_task_queue:
            raise RuntimeError("Activity submission requires a task queue")
        try:
            handle = await client.start_activity(
                name,
                request,
                result_type=result_type,
                id=str(activity_id),
                task_queue=self._temporal_task_queue,
                id_conflict_policy=ActivityIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=ActivityIDReusePolicy.REJECT_DUPLICATE,
                schedule_to_close_timeout=_ACTIVITY_TIMEOUT,
                start_to_close_timeout=_ACTIVITY_TIMEOUT,
                heartbeat_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
                rpc_timeout=_TEMPORAL_RPC_TIMEOUT,
            )
        except ActivityAlreadyStartedError as error:
            # Retained terminal rejection supplies the precise execution to reuse.
            handle = client.get_activity_handle(
                str(activity_id),
                run_id=error.run_id,
                result_type=result_type,
            )
        try:
            description = await self._describe(
                request.session_id,
                activity_id,
                handle.run_id,
                expected_type=name,
                include_outcome=False,
            )
        except RPCError:
            raise SessionExecutionError(
                request.session_id,
                activity_id,
                handle.run_id,
                "UnableToConfirm",
            ) from None
        return client.get_activity_handle(
            str(activity_id),
            run_id=description.activity_run_id,
            result_type=result_type,
        )

    async def _wait_session(
        self, session_id: UUID, activity_id: UUID, handle: ActivityHandle[SessionRecord]
    ) -> SessionRecord:
        try:
            return await handle.result(rpc_timeout=_TEMPORAL_RPC_TIMEOUT)
        except ActivityFailureError as error:
            raise SessionExecutionError(
                session_id, activity_id, handle.run_id, failure_category(error.cause)
            ) from None
        except RPCError as error:
            raise SessionExecutionError(
                session_id,
                activity_id,
                handle.run_id,
                "OutcomeUnavailable"
                if error.status == RPCStatusCode.NOT_FOUND
                else "UnableToConfirm",
            ) from None

    async def create_session(self, data: CreateSession, *, activity_id: UUID) -> SessionRecord:
        """Wait for native create; canceling this await leaves the Activity running."""
        session_id = data.id or activity_id
        try:
            handle = await self._start(
                CREATE,
                CreateSessionActivity(session_id=session_id, request=data),
                activity_id,
                SessionRecord,
            )
        except RPCError:
            raise SessionExecutionError(session_id, activity_id, None, "UnableToConfirm") from None
        return await self._wait_session(session_id, activity_id, handle)

    async def start_runner(
        self,
        session_id: UUID,
        *,
        activity_id: UUID,
        user_prompt: str,
    ) -> ActivityHandle[str]:
        """Return an accepted native execution; Worker admission can still fail."""
        if not isinstance(user_prompt, str):
            raise ValueError("user_prompt must be a string")
        return await self._start(
            RUN,
            RunAgentActivity(session_id=session_id, user_prompt=user_prompt),
            activity_id,
            str,
        )

    async def close_session(self, session_id: UUID, *, activity_id: UUID) -> SessionRecord:
        try:
            handle = await self._start(
                CLOSE,
                SessionActivityRequest(session_id=session_id),
                activity_id,
                SessionRecord,
            )
        except RPCError:
            raise SessionExecutionError(session_id, activity_id, None, "UnableToConfirm") from None
        return await self._wait_session(session_id, activity_id, handle)

    async def resume_runner(self, session_id: UUID, *, activity_id: UUID) -> ActivityHandle[str]:
        """Submit explicit recovery; the Worker reads the latest consistent checkpoint."""
        return await self._start(
            RESUME, SessionActivityRequest(session_id=session_id), activity_id, str
        )

    async def get_activity(
        self,
        session_id: UUID,
        activity_id: UUID,
        *,
        run_id: str | None = None,
    ) -> SessionActivity:
        """Read Temporal directly, even before create has inserted a session row."""
        try:
            description = await self._describe(session_id, activity_id, run_id)
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                raise LookupError("Activity not found; its outcome cannot be confirmed") from None
            raise
        result = await description.result(
            str if description.activity_type in (RUN, RESUME) else SessionRecord
        )
        failure = await description.outcome_failure()
        assert description.activity_run_id is not None
        return SessionActivity(
            session_id=session_id,
            activity_id=activity_id,
            run_id=description.activity_run_id,
            activity_type=description.activity_type,
            status=description.status.name,
            run_state=description.run_state.name if description.run_state is not None else None,
            result=result,
            error=failure_category(failure) if failure is not None else None,
        )

    async def wait_session_activity(
        self,
        session_id: UUID,
        activity_id: UUID,
        *,
        run_id: str | None = None,
        activity_type: str,
    ) -> SessionRecord:
        """Recover an attempted create/close by lookup only; never resubmit it."""
        if activity_type not in (CREATE, CLOSE):
            raise ValueError("Only create and close return a session")
        description = await self._describe(
            session_id,
            activity_id,
            run_id,
            expected_type=activity_type,
            include_outcome=False,
        )
        handle = self._client().get_activity_handle(
            str(activity_id),
            run_id=description.activity_run_id,
            result_type=SessionRecord,
        )
        return await self._wait_session(session_id, activity_id, handle)

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
        from kapy.runner.checkpoints import decode_state, require_new_prompt

        async with self._session_factory() as db, db.begin():
            repo = SessionRepository(db)
            record = await repo.update_session(session_id, data.model_dump(exclude_unset=True))
            # The conditional edit holds the row through validation and commit.
            require_new_prompt(decode_state(await repo.read_runner_state(session_id)))
            return record

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

"""Durable session operations, configuration and immutable output replay.

Database ownership is claimed before Temporal submission. Every external call is
outside its short transaction. Same-key requests reuse frozen input and permanent
outcomes; uncertain dispatch never releases the session. Workers own all scopes,
model/tool resources and execution. Each live call owns its subscription/read task.
"""

import asyncio
import math
from collections.abc import AsyncGenerator
from datetime import timedelta
from uuid import UUID, uuid4

from pydantic_ai.exceptions import UserError
from sqlalchemy import or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import col, select
from temporalio.client import ActivityExecutionStatus, Client
from temporalio.common import ActivityIDConflictPolicy, ActivityIDReusePolicy, RetryPolicy
from temporalio.exceptions import ActivityAlreadyStartedError
from temporalio.service import RPCError

from kapy.agent_history import HistoryMessage, HistoryRepository
from kapy.agent_output import AgentOutputService, MessageCommitted, OutputEvent, TextDelta
from kapy.control.models.repository import ModelRepository
from kapy.control.models.runtime import resolve_classes, validate_settings
from kapy.control.types import utc_now
from kapy.layers.registry import layer_registry
from kapy.lifecycle import LifecycleStatus
from kapy.pagination import BeforeSeqPagination, Page, validate_pagination
from kapy.runner.types import ExecutionConfig, RunnerInput
from pydantic_ai_layer_protocol import dump_object

from .models import SessionRow
from .operations import ACTIVE, TERMINAL, OperationRepository, SessionOperationRow, public_operation
from .repository import SessionRepository
from .types import (
    CreateSession,
    OperationConflict,
    OperationFailed,
    OperationKind,
    OperationPhase,
    OwnershipLost,
    SessionAlreadyExists,
    SessionBusy,
    SessionOperation,
    SessionRecord,
    UpdateSession,
)

_TEMPORAL_RPC_TIMEOUT = timedelta(seconds=10)
_OPERATION_TIMEOUT = timedelta(hours=1)
_ACTIVITY_NAMES = {
    OperationKind.CREATE: "kapy.create_session",
    OperationKind.RUN: "kapy.run_agent",
    OperationKind.CLOSE: "kapy.close_session",
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
        if self._temporal_client is None or not self._temporal_task_queue:
            raise RuntimeError("Session mutations require a Temporal client and task queue")
        return self._temporal_client

    async def _existing(
        self,
        activity_id: UUID,
        kind: OperationKind,
        request: dict,
        session_id: UUID | None = None,
    ) -> SessionOperation | None:
        async with self._session_factory() as db, db.begin():
            row = await db.get(SessionOperationRow, activity_id)
            if row is None:
                return None
            if (
                row.kind != kind
                or row.request != request
                or (session_id is not None and row.session_id != session_id)
            ):
                raise OperationConflict("Activity ID belongs to different intent")
            result = public_operation(row)
        return await self._reconcile(result)

    async def create_session(self, data: CreateSession, *, activity_id: UUID) -> SessionRecord:
        """Create once; cancellation of this await leaves the remote operation intact."""
        self._client()
        request = data.model_dump(mode="json")
        existing = await self._existing(activity_id, OperationKind.CREATE, request)
        if existing is None:
            try:
                configs = layer_registry.prepare_configs(request["layers"])
                for name in configs:
                    definition = layer_registry.definitions[name]
                    if definition.enter_scope is None:
                        definition.state_type.model_validate({})
            except UserError as error:
                raise ValueError(str(error)) from error
            layers = {name: dump_object(config) for name, config in configs.items()}
            session_id = data.id or uuid4()
            now = utc_now()
            operation = SessionOperationRow(
                activity_id=activity_id,
                session_id=session_id,
                kind=OperationKind.CREATE,
                request=request,
                # Worker validates the same fixed request, rather than running
                # deterministic normalizers again on already-normalized Config.
                input={"layers": request["layers"]},
                created_at=now,
                deadline=now + _OPERATION_TIMEOUT,
            )
            async with self._session_factory() as db, db.begin():
                row, fresh = await OperationRepository(db).insert(operation)
                if fresh:
                    inserted = (
                        await db.execute(
                            insert(SessionRow)
                            .values(
                                **data.model_dump(exclude={"id", "layers"}),
                                id=session_id,
                                layers=layers,
                                layer_states={},
                                status=LifecycleStatus.CREATING,
                                current_activity_id=activity_id,
                                created_at=now,
                                updated_at=now,
                            )
                            .on_conflict_do_nothing()
                            .returning(col(SessionRow.id))
                        )
                    ).scalar_one_or_none()
                    if inserted is None:
                        raise SessionAlreadyExists(session_id)
                existing = public_operation(row)
        operation = await self._dispatch(existing)
        return await self._wait_session(operation)

    async def start_runner(
        self,
        session_id: UUID,
        *,
        activity_id: UUID,
        user_prompt: str,
    ) -> SessionOperation:
        """Freeze input and claim STARTING; same-key confirmation never re-resolves config."""
        self._client()
        if not isinstance(user_prompt, str):
            raise ValueError("user_prompt must be a string")
        request = {"user_prompt": user_prompt}
        existing = await self._existing(activity_id, OperationKind.RUN, request, session_id)
        if existing is None:
            await self._reconcile_current(session_id)
            now = utc_now()
            async with self._session_factory() as db, db.begin():
                row, fresh = await OperationRepository(db).insert(
                    SessionOperationRow(
                        activity_id=activity_id,
                        session_id=session_id,
                        kind=OperationKind.RUN,
                        request=request,
                        created_at=now,
                        deadline=now + _OPERATION_TIMEOUT,
                    )
                )
                if fresh:
                    session = await self._claim_session(db, session_id, activity_id, close=False)
                    catalog = ModelRepository(db)
                    model = await catalog.get_model(session.provider_id, session.model_name)
                    provider = await catalog.get_provider_config(session.provider_id)
                    _, model_class = resolve_classes(provider)
                    settings = validate_settings(
                        model_class, model.settings | session.model_settings
                    )
                    data = RunnerInput(
                        session_id=session_id,
                        runner_state=session.runner_state,
                        layers=layer_registry.load(session.layers, session.layer_states),
                        user_prompt=user_prompt,
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
                    row.input = data.model_dump(mode="json")
                    await db.flush()
                existing = public_operation(row)
        return await self._dispatch(existing)

    async def close_session(self, session_id: UUID, *, activity_id: UUID) -> SessionRecord:
        """Claim cleanup only when no live owner exists; repeat failed cleanup with a new ID."""
        self._client()
        request: dict = {}
        existing = await self._existing(activity_id, OperationKind.CLOSE, request, session_id)
        if existing is None:
            await self._reconcile_current(session_id)
            now = utc_now()
            async with self._session_factory() as db, db.begin():
                row, fresh = await OperationRepository(db).insert(
                    SessionOperationRow(
                        activity_id=activity_id,
                        session_id=session_id,
                        kind=OperationKind.CLOSE,
                        request=request,
                        created_at=now,
                        deadline=now + _OPERATION_TIMEOUT,
                    )
                )
                if fresh:
                    session = await db.get(SessionRow, session_id)
                    if session is None:
                        raise LookupError(f"Session {session_id} does not exist")
                    if session.status == LifecycleStatus.CLOSED:
                        row.phase = OperationPhase.SUCCEEDED
                        row.result = SessionRecord.model_validate(session).model_dump(mode="json")
                    else:
                        session = await self._claim_session(db, session_id, activity_id, close=True)
                        row.input = {"layers": session.layers, "layer_states": session.layer_states}
                    await db.flush()
                existing = public_operation(row)
        operation = await self._dispatch(existing)
        return await self._wait_session(operation)

    async def _claim_session(
        self,
        db: AsyncSession,
        session_id: UUID,
        activity_id: UUID,
        *,
        close: bool,
    ) -> SessionRow:
        statement = update(SessionRow).where(col(SessionRow.id) == session_id)
        if close:
            terminal = select(SessionOperationRow.activity_id).where(
                col(SessionOperationRow.phase).in_(TERMINAL)
            )
            statement = statement.where(
                or_(
                    col(SessionRow.status) == LifecycleStatus.READY,
                    (col(SessionRow.status) == LifecycleStatus.CLOSING)
                    & or_(
                        col(SessionRow.current_activity_id).is_(None),
                        col(SessionRow.current_activity_id).in_(terminal),
                    ),
                )
            )
        else:
            statement = statement.where(col(SessionRow.status) == LifecycleStatus.READY)
        row = (
            await db.execute(
                statement.values(
                    status=LifecycleStatus.CLOSING if close else LifecycleStatus.STARTING,
                    current_activity_id=activity_id,
                    updated_at=utc_now(),
                )
                .returning(SessionRow)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        if row is None:
            if await db.get(SessionRow, session_id) is None:
                raise LookupError(f"Session {session_id} does not exist")
            raise SessionBusy(f"Session {session_id} cannot be claimed")
        return row

    async def _dispatch(self, operation: SessionOperation) -> SessionOperation:
        if operation.phase in TERMINAL:
            return operation
        if operation.phase == OperationPhase.RUNNING:
            return await self._reconcile(operation)
        client = self._client()
        assert self._temporal_task_queue is not None
        remaining = operation.deadline - utc_now()
        if remaining <= timedelta():
            return await self._reconcile(operation)
        try:
            handle = await client.start_activity(
                _ACTIVITY_NAMES[operation.kind],
                operation.activity_id,
                result_type=str if operation.kind == OperationKind.RUN else SessionRecord,
                id=str(operation.activity_id),
                task_queue=self._temporal_task_queue,
                id_conflict_policy=ActivityIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=ActivityIDReusePolicy.REJECT_DUPLICATE,
                schedule_to_close_timeout=remaining,
                start_to_close_timeout=remaining,
                heartbeat_timeout=timedelta(seconds=10),
                retry_policy=RetryPolicy(maximum_attempts=1),
                rpc_timeout=_TEMPORAL_RPC_TIMEOUT,
            )
        except ActivityAlreadyStartedError:
            return await self._reconcile(operation)
        if handle.run_id is not None:
            async with self._session_factory() as db, db.begin():
                await OperationRepository(db).bind_run_id(operation.activity_id, handle.run_id)
        return await self.get_operation(operation.session_id, operation.activity_id)

    async def _reconcile_current(self, session_id: UUID) -> None:
        async with self._session_factory() as db, db.begin():
            session = await db.get(SessionRow, session_id)
            if session is None:
                raise LookupError(f"Session {session_id} does not exist")
            row = (
                None
                if session.current_activity_id is None
                else await db.get(SessionOperationRow, session.current_activity_id)
            )
            operation = None if row is None else public_operation(row)
        if operation is not None:
            await self._reconcile(operation)

    async def _reconcile(self, operation: SessionOperation) -> SessionOperation:
        if operation.phase in TERMINAL:
            return operation
        failed = operation.deadline <= utc_now()
        if not failed and self._temporal_client is not None:
            try:
                description = await self._temporal_client.get_activity_handle(
                    str(operation.activity_id),
                    run_id=operation.run_id,
                ).describe(rpc_timeout=_TEMPORAL_RPC_TIMEOUT)
            except RPCError:
                # Unknown execution is not permission to release or re-execute.
                pass
            else:
                if description.activity_run_id is not None:
                    async with self._session_factory() as db, db.begin():
                        await OperationRepository(db).bind_run_id(
                            operation.activity_id, description.activity_run_id
                        )
                failed = description.status not in (
                    ActivityExecutionStatus.RUNNING,
                    ActivityExecutionStatus.PAUSED,
                )
        if failed:
            try:
                async with self._session_factory() as db, db.begin():
                    await OperationRepository(db).fail(
                        operation.activity_id,
                        "Session operation ended without a committed result",
                        expired=True,
                    )
            except OwnershipLost:
                pass
        async with self._session_factory() as db, db.begin():
            return public_operation(await OperationRepository(db).get(operation.activity_id))

    async def get_operation(self, session_id: UUID, activity_id: UUID) -> SessionOperation:
        async with self._session_factory() as db, db.begin():
            row = await OperationRepository(db).get(activity_id)
            if row.session_id != session_id:
                raise LookupError("Session operation does not exist")
            operation = public_operation(row)
        return await self._reconcile(operation)

    async def _wait_session(self, operation: SessionOperation) -> SessionRecord:
        while operation.phase in ACTIVE:
            await asyncio.sleep(0.05)
            operation = await self.get_operation(operation.session_id, operation.activity_id)
        if operation.phase == OperationPhase.FAILED:
            raise OperationFailed(operation)
        assert isinstance(operation.result, SessionRecord)
        return operation.result

    async def is_runner_running(self, session_id: UUID) -> bool:
        async with self._session_factory() as db, db.begin():
            record = await SessionRepository(db).get_session(session_id)
            return record.status in (LifecycleStatus.STARTING, LifecycleStatus.RUNNING)

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

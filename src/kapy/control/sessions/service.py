"""Session configuration, durable execution admission and immutable output replay.

Short transactions own configuration. Admission holds the session row lock through
bounded Temporal RPCs; the Worker owns model execution and resources. Native layer
business scopes use the separate session lease for creation/close with fencing.
Each live call owns its subscription/read task; no transaction spans a yield.
"""

import asyncio
import logging
import math
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from copy import deepcopy
from datetime import timedelta
from uuid import UUID, uuid4

import anyio
from pydantic import BaseModel
from pydantic_ai.exceptions import UserError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from kapy.agent_history import HistoryMessage, HistoryRepository
from kapy.agent_output import AgentOutputService, MessageCommitted, OutputEvent, TextDelta
from kapy.control.models.repository import ModelRepository
from kapy.control.models.runtime import resolve_classes, validate_settings
from kapy.control.types import utc_now
from kapy.layers import load_layers
from kapy.layers.modules import dump_object, prepare_layer_configs
from kapy.layers.registry import LAYERS
from kapy.layers.scopes import retry_scope
from kapy.layers.types import JsonObject
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.pagination import BeforeSeqPagination, Page, validate_pagination
from kapy.runner_duarable import DurableExecutionConfig, RunnerInput, RunnerWorkflow
from kapy.session_lease import SessionBusy, SessionLease, open_session_lease

from .repository import SessionRepository, lock_session
from .types import (
    CreateSession,
    SessionAlreadyExists,
    SessionCreationError,
    SessionRecord,
    UpdateSession,
)


class DurableRunnerConflict(Exception):
    """The preceding Workflow did not complete successfully; do not restart old state."""


_TEMPORAL_RPC_TIMEOUT = timedelta(seconds=10)
_logger = logging.getLogger(__name__)


class SessionService:
    """Borrow session/output factories and Temporal; own no model or engine.

    Model configuration is fixed when admission starts a Workflow. Heartbeat policy
    belongs only to native layer scope creation/close, not runner execution.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        output_service: AgentOutputService | None = None,
        temporal_client: Client | None = None,
        temporal_task_queue: str | None = None,
        heartbeat_interval: float = 10.0,
        heartbeat_timeout: float = 60.0,
        takeover_grace_period: float = 30.0,
        live_poll_interval: float = 5.0,
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
        self._temporal_client = temporal_client
        self._temporal_task_queue = temporal_task_queue
        self._live_poll_interval = live_poll_interval
        self._session_factory = session_factory
        self._output_service = output_service
        self._heartbeat_interval = heartbeat_interval
        self._heartbeat_timeout = heartbeat_timeout
        self._takeover_grace_period = takeover_grace_period

    async def create_session(self, data: CreateSession) -> SessionRecord:
        """Reserve identity/Config, enter under its lease, then publish complete State.

        Hook I/O is outside transactions. Failed initialization closes all configured
        scopes with None, including partial and uncalled enters. A cancelled cleanup
        hook leaves closing and does not prevent the other scopes' compensation.
        A ready COMMIT error is resolved only after that transaction ends:
        confirmed ready is kept;
        creating is closed. An unavailable database leaves an unconfirmed outcome.
        Restarted hosts never replay enter. Callers retain the fixed ID to query/close.
        """
        try:
            configs = prepare_layer_configs(data.layers, LAYERS)
        except UserError as exc:
            raise ValueError(str(exc)) from exc
        data = data.model_copy(
            update={"layers": {name: dump_object(config) for name, config in configs.items()}}
        )
        session_id = data.id or uuid4()
        registered = False
        states: dict[str, JsonObject] = {}
        # Validate default States before reservation; enter-backed States may have
        # required fields and need not be constructible until the hook returns.
        for name in configs:
            if LAYERS[name].enter_scope is None:
                states[name] = dump_object(LAYERS[name].state_type.model_validate({}))
        try:
            async with self._lease(session_id) as lease:
                # Never call enter unless registration COMMIT was acknowledged.
                async with self._scope_transaction(lease) as db:
                    await SessionRepository(db).register_creating(data, session_id=session_id)
                    await db.flush()
                registered = True
                try:
                    for name, config in configs.items():
                        enter = LAYERS[name].enter_scope
                        if enter is None:
                            continue

                        async def attempt(enter=enter, config=config):
                            lease.check()
                            return await enter(session_id, config.model_copy(deep=True))

                        returned = await retry_scope(attempt)
                        state_type = LAYERS[name].state_type
                        if not isinstance(returned, state_type):
                            raise TypeError(f"Layer {name!r} enter_scope must return its State")
                        states[name] = dump_object(state_type.model_validate(dump_object(returned)))
                    async with self._scope_transaction(lease) as db:
                        record = await SessionRepository(db).publish_ready(session_id, states)
                    return record
                except BaseException as error:
                    # The previous transaction's close has finished before probing
                    # the authoritative row. Cancellation remains a cancellation,
                    # even when this probe confirms successful creation.
                    with anyio.CancelScope(shield=True):
                        try:
                            resolved = await self._close_owned(
                                session_id, lease, failed_creation=True
                            )
                        except BaseException:
                            _logger.exception(
                                "Creation outcome/compensation failed for session %s", session_id
                            )
                        else:
                            if resolved.status == LifecycleStatus.READY and isinstance(
                                error, Exception
                            ):
                                return resolved
                    raise
        except SessionAlreadyExists as error:
            if not registered and error.session_id == session_id:
                raise
            raise SessionCreationError(session_id) from error
        except SessionBusy as error:
            if registered:
                raise SessionCreationError(session_id) from error
            raise SessionAlreadyExists(session_id) from error
        except Exception as error:
            raise SessionCreationError(session_id) from error
        except BaseException as error:
            error.add_note(f"Session creation identity: {session_id}")
            raise

    def _lease(self, session_id: UUID) -> AbstractAsyncContextManager[SessionLease]:
        return open_session_lease(
            session_id,
            session_factory=self._session_factory,
            heartbeat_interval=self._heartbeat_interval,
            heartbeat_timeout=self._heartbeat_timeout,
            takeover_grace_period=self._takeover_grace_period,
        )

    @asynccontextmanager
    async def _scope_transaction(
        self, lease: SessionLease, *, budget: float = 5
    ) -> AsyncIterator[AsyncSession]:
        """Fence, commit and close in the owner task before its lease can be released."""
        db = self._session_factory()
        try:
            with anyio.fail_after(budget):
                await lease.lock_owned(db)
                yield db
                await db.commit()
        finally:
            with anyio.fail_after(5, shield=True):
                await db.close()

    async def close_session(self, session_id: UUID) -> SessionRecord:
        """Acquire exclusive ownership before deciding closing; Busy changes no state.

        With a Temporal Client, ready/closing sessions check remote activity under
        the row lock before changing lifecycle. Creating skips the query so abandoned
        scopes can be cleaned independently of Temporal; closed returns directly.
        Query failures propagate; close never stops a Workflow. Close never changes
        execution. Failures retain the irreversible closing
        decision and completed cleanup progress for explicit retry.
        Registered layer scopes are cleaned under the lease;
        fencing cannot revoke already-issued external requests from a lost owner.
        Optional layer exits save mutated State after every attempt, even on
        error/cancellation. Only RetryableScopeError permits an automatic retry;
        a later close can repeat earlier successful exits, which must be idempotent.
        Abandoned creating scopes have no saved State and exit with None. Ordinary
        exit failures are recorded while the remaining configured exits are tried.
        """
        async with self._lease(session_id) as lease:
            return await self._close_owned(session_id, lease)

    async def _close_owned(
        self, session_id: UUID, lease: SessionLease, *, failed_creation: bool = False
    ) -> SessionRecord:
        """Resolve abandoned creation or perform close while retaining the same lease."""
        # Admission includes the existing ten-second Workflow query; preserve that
        # RPC budget rather than timing it out at the shorter DB-only deadline.
        async with self._scope_transaction(
            lease, budget=_TEMPORAL_RPC_TIMEOUT.total_seconds() + 5
        ) as db:
            row = await lock_session(db, session_id)
            if row.status == LifecycleStatus.CLOSED or (
                failed_creation and row.status == LifecycleStatus.READY
            ):
                return SessionRecord.model_validate(row)
            if (
                not failed_creation
                and row.status != LifecycleStatus.CREATING
                and self._temporal_client is not None
                and await self.is_durable_runner_running(session_id)
            ):
                raise SessionBusy(f"Session {session_id} has an active Workflow")
            configs = prepare_layer_configs(row.layers, LAYERS)
            # READY always has a complete validated snapshot. An abandoned creating
            # record stores no States at all; do not fabricate defaults for it.
            states = deepcopy(row.layer_states)
            if row.status == LifecycleStatus.READY or states:
                if states.keys() != row.layers.keys():
                    raise ValueError("Saved layer states must match the configured layer names")
                states = {
                    name: dump_object(LAYERS[name].state_type.model_validate(value))
                    for name, value in states.items()
                }
            row.status, row.updated_at = LifecycleStatus.CLOSING, utc_now()
        error: Exception | asyncio.CancelledError | None = None
        for name, config in reversed(configs.items()):
            try:
                await self._exit_layer_scope(session_id, name, config, states.get(name), lease)
            except asyncio.CancelledError as caught:
                if not failed_creation:
                    raise
                _logger.exception(
                    "Scope compensation cancelled for session %s layer %s", session_id, name
                )
                error = error or caught
            except Exception as caught:
                _logger.exception("Scope cleanup failed for session %s layer %s", session_id, name)
                error = error or caught
        if error is not None:
            raise error
        async with self._scope_transaction(lease) as db:
            row = await lock_session(db, session_id)
            if row.status != LifecycleStatus.CLOSING:
                raise LifecycleError(f"Session {session_id} is {row.status}")
            row.status, row.updated_at = LifecycleStatus.CLOSED, utc_now()
            return SessionRecord.model_validate(row)

    async def _exit_layer_scope(
        self,
        session_id: UUID,
        name: str,
        config: BaseModel,
        saved_state: JsonObject | None,
        lease: SessionLease,
    ) -> None:
        """Persist valid exit progress under ownership before propagating or retrying."""
        exit_hook = LAYERS[name].exit_scope
        if exit_hook is None:
            return
        state_type = LAYERS[name].state_type
        state = None if saved_state is None else state_type.model_validate(deepcopy(saved_state))

        async def attempt() -> None:
            lease.check()
            await exit_hook(session_id, config.model_copy(deep=True), state)

        async def save_progress() -> None:
            nonlocal state
            if state is None:
                return
            # Model instances do not revalidate mutations by default. Round-trip
            # through JSON so persisted progress and the next attempt are validated.
            state = state_type.model_validate(dump_object(state))
            saved = dump_object(state)
            with anyio.CancelScope(shield=True):
                async with self._scope_transaction(lease) as db:
                    await SessionRepository(db).save_layer_cleanup(session_id, name, saved)

        await retry_scope(attempt, after_attempt=save_progress)

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
        async with self._session_factory.begin() as db:
            return await SessionRepository(db).update_session(session_id, values)

    async def _durable_status(self, session_id: UUID) -> WorkflowExecutionStatus | None:
        if self._temporal_client is None:
            raise RuntimeError("Durable execution requires a Temporal client")
        try:
            description = await self._temporal_client.get_workflow_handle(
                f"kapy-runner:{session_id}"
            ).describe(rpc_timeout=_TEMPORAL_RPC_TIMEOUT)
        except RPCError as error:
            if error.status == RPCStatusCode.NOT_FOUND:
                return None
            raise
        return description.status

    async def is_durable_runner_running(self, session_id: UUID) -> bool:
        """Observe Temporal execution; only NOT_FOUND means absent, RPC failures propagate."""
        return await self._durable_status(session_id) == WorkflowExecutionStatus.RUNNING

    async def start_durable_runner(
        self, session_id: UUID, *, user_prompt: str
    ) -> WorkflowHandle[RunnerWorkflow, str]:
        """Submit one text prompt, returning after Temporal confirms the start.

        Borrow the Client and serialize admission/close with a session row lock,
        held only through bounded RPCs. A new execution may follow only COMPLETED,
        preserving recorded
        history across replay. A failed/ambiguous RPC never triggers a new attempt
        here; callers query the fixed Workflow ID to discover accepted execution.
        The Worker owns model resources and state saving; cancelling this await
        does not cancel a Workflow.
        """
        if self._temporal_client is None or not self._temporal_task_queue:
            raise RuntimeError("Durable execution requires a Temporal client and task queue")
        if not isinstance(user_prompt, str):
            raise ValueError("user_prompt must be a string")
        async with self._session_factory.begin() as db:
            row = await lock_session(db, session_id)
            if row.status != LifecycleStatus.READY:
                raise LifecycleError(f"Session {session_id} is {row.status}")
            if row.layer_states.keys() != row.layers.keys():
                raise ValueError("Saved layer states must match the configured layer names")
            status = await self._durable_status(session_id)
            if status == WorkflowExecutionStatus.RUNNING:
                raise SessionBusy(f"Session {session_id} has an active Workflow")
            if status is not None and status != WorkflowExecutionStatus.COMPLETED:
                raise DurableRunnerConflict(f"Previous Workflow ended with {status.name}")
            repo = ModelRepository(db)
            model = await repo.get_model(row.provider_id, row.model_name)
            provider = await repo.get_provider_config(row.provider_id)
            _, model_class = resolve_classes(provider)
            settings = validate_settings(model_class, model.settings | row.model_settings)
            state, version = await SessionRepository(db).read_runner_state(session_id)
            data = RunnerInput(
                session_id=session_id,
                runner_state=state,
                runner_state_version=version,
                layers=load_layers(row.layers, row.layer_states, LAYERS),
                user_prompt=user_prompt,
                config=DurableExecutionConfig(
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
            try:
                return await self._temporal_client.start_workflow(
                    RunnerWorkflow.run,
                    data,
                    id=f"kapy-runner:{session_id}",
                    task_queue=self._temporal_task_queue,
                    id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                    id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
                    rpc_timeout=_TEMPORAL_RPC_TIMEOUT,
                )
            except WorkflowAlreadyStartedError as error:
                raise SessionBusy(f"Session {session_id} has an active Workflow") from error

    async def read_history(
        self, session_id: UUID, *, before_seq: int | None = None, limit: int = 100
    ) -> Page[HistoryMessage]:
        """Read the latest page before an exclusive cursor, ordered oldest to newest."""
        BeforeSeqPagination(before_seq=before_seq, limit=limit)
        async with self._session_factory.begin() as db:
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
            async with self._session_factory.begin() as db:
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

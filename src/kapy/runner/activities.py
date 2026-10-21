"""Worker-owned session Activities; native Temporal executions own all resources.

Only executing Workers acquire database ownership. The complete native identity
fences progress; Temporal alone owns scheduling, timeouts, outcomes and inputs.
No model/tool/hook or Temporal RPC runs inside an ownership transaction.
"""

import asyncio
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from typing import Literal, cast
from uuid import UUID

import anyio
from pydantic_ai.settings import ModelSettings
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode

from kapy.agent_history import HistoryRepository
from kapy.agent_output import AgentOutputService
from kapy.control.models.repository import ModelRepository
from kapy.control.models.runtime import (
    build_model,
    build_provider,
    resolve_classes,
    validate_settings,
)
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.control.sessions.temporal import TERMINAL_STATUSES, failure_category
from kapy.control.sessions.types import (
    CreateSessionActivity,
    OwnershipLost,
    RunAgentActivity,
    SessionActivityRequest,
    SessionBusy,
    SessionRecord,
)
from kapy.layers.registry import layer_registry
from kapy.lifecycle import LifecycleError, LifecycleStatus
from pydantic_ai_layer_protocol import dump_object

from .agent import agent
from .checkpoints import (
    Checkpoint,
    DoneNext,
    decode_state,
    require_new_prompt,
    state_messages,
    validate_history,
)
from .output import handle_deltas
from .ownership import fenced_agent
from .recording import Recorder
from .scopes import check_owner, close_scopes, create_scopes
from .types import ExecutionConfig, RunnerDeps, RunnerInput


class RunnerActivity:
    """Borrow Worker resources; each invocation owns clients, State and heartbeats."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        output_service: AgentOutputService,
        temporal_client: Client,
    ) -> None:
        self._session_factory = session_factory
        self._output_service = output_service
        self._temporal_client = temporal_client

    @asynccontextmanager
    async def _invocation(
        self, session_id: UUID, *, mode: Literal["scope", "start", "resume"] = "scope"
    ):
        info = activity.info()
        if info.in_workflow or not info.activity_run_id:
            raise ApplicationError("StandaloneIdentityRequired", non_retryable=True)
        activity_id, run_id = UUID(info.activity_id), info.activity_run_id
        heartbeat = asyncio.create_task(self._heartbeat())
        try:
            yield activity_id, run_id
        except BaseException as error:
            # Resource contexts have exited before failure releases the writer.
            # A lost success-commit reply cannot match the cleared identity.
            with anyio.move_on_after(15, shield=True):
                try:
                    async with self._session_factory() as db, db.begin():
                        repo = SessionRepository(db)
                        statuses = (
                            (LifecycleStatus.READY,)
                            if mode != "scope"
                            else (
                                LifecycleStatus.CREATING,
                                LifecycleStatus.READY,
                                LifecycleStatus.CLOSING,
                            )
                        )
                        row = await repo.guard(session_id, activity_id, run_id, statuses)
                        status = LifecycleStatus.CLOSING
                        if mode == "resume":
                            status = LifecycleStatus.READY
                        elif mode == "start":
                            # Start admitted only empty/legacy/done. An unfinished
                            # committed checkpoint therefore belongs to this run.
                            state = decode_state(row.runner_state)
                            if isinstance(state, Checkpoint) and not isinstance(
                                state.next, DoneNext
                            ):
                                status = LifecycleStatus.READY
                        await repo.complete(
                            session_id,
                            activity_id,
                            run_id,
                            statuses,
                            status,
                        )
                except OwnershipLost:
                    pass
                except Exception:
                    activity.logger.warning("Unable to release session writer after failure")
            if isinstance(error, asyncio.CancelledError):
                raise
            raise ApplicationError(
                failure_category(error),
                type=failure_category(error),
                non_retryable=True,
            ) from None
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    @staticmethod
    async def _heartbeat() -> None:
        while True:
            activity.heartbeat()
            await asyncio.sleep(2)

    @activity.defn(name="kapy.create_session")
    async def create(self, request: CreateSessionActivity) -> SessionRecord:
        async with self._invocation(request.session_id) as (activity_id, run_id):
            if request.request.id is not None and request.request.id != request.session_id:
                raise ValueError("Creation identities must agree")
            # All Config/default validation precedes insertion and every enter.
            configs = layer_registry.prepare_configs(request.request.layers)
            for name in configs:
                definition = layer_registry.definitions[name]
                if definition.enter_scope is None:
                    definition.state_type.model_validate({})
            row = SessionRow(
                **request.request.model_dump(exclude={"id", "layers"}),
                id=request.session_id,
                layers={name: dump_object(config) for name, config in configs.items()},
                layer_states={},
                status=LifecycleStatus.CREATING,
                current_activity_id=activity_id,
                current_activity_run_id=run_id,
            )
            async with self._session_factory() as db, db.begin():
                await SessionRepository(db).create(row)
            return await create_scopes(
                self._session_factory,
                activity_id,
                run_id,
                request.session_id,
                configs,
            )

    async def _claim_close(self, session_id: UUID, activity_id: UUID, run_id: str) -> SessionRow:
        async with self._session_factory() as db, db.begin():
            repo = SessionRepository(db)
            row = await repo._session(session_id)
            if row.current_activity_id is None:
                if row.status == LifecycleStatus.CLOSED:
                    return row
                claimed = await repo.claim_close_or_run(session_id, activity_id, run_id, close=True)
                if claimed is not None:
                    return claimed
                raise SessionBusy("Session cannot be closed")
            assert row.current_activity_run_id is not None
            previous = (row.current_activity_id, row.current_activity_run_id, row.status)
        await self._confirm_terminal(previous[0], previous[1])
        async with self._session_factory() as db, db.begin():
            repo = SessionRepository(db)
            claimed = await repo.claim_close_or_run(
                session_id,
                activity_id,
                run_id,
                close=True,
                previous=previous,
            )
            if claimed is not None:
                return claimed
            row = await repo._session(session_id)
            if row.current_activity_id is None:
                if row.status == LifecycleStatus.CLOSED:
                    return row
                claimed = await repo.claim_close_or_run(session_id, activity_id, run_id, close=True)
                if claimed is not None:
                    return claimed
            raise SessionBusy("Another execution changed the session")

    async def _confirm_terminal(self, activity_id: UUID, run_id: str) -> None:
        """Allow explicit close/resume takeover only after exact native confirmation.

        A missing execution permits CAS; it does not prove a business result.
        Call outside transactions. Ordinary admission and queries never reconcile.
        """
        try:
            description = await self._temporal_client.get_activity_handle(
                str(activity_id), run_id=run_id
            ).describe(rpc_timeout=timedelta(seconds=10))
        except RPCError as error:
            if error.status != RPCStatusCode.NOT_FOUND:
                raise ApplicationError("UnableToConfirm", type="UnableToConfirm") from None
        else:
            if description.status not in TERMINAL_STATUSES:
                raise SessionBusy("Previous native execution may still be active")

    @activity.defn(name="kapy.close_session")
    async def close(self, request: SessionActivityRequest) -> SessionRecord:
        async with self._invocation(request.session_id) as (activity_id, run_id):
            row = await self._claim_close(request.session_id, activity_id, run_id)
            if row.status == LifecycleStatus.CLOSED:
                return SessionRecord.model_validate(row)
            return await close_scopes(
                self._session_factory,
                activity_id,
                run_id,
                row.id,
                {"layers": row.layers, "layer_states": row.layer_states},
            )

    @activity.defn(name="kapy.run_agent")
    async def run(self, request: RunAgentActivity) -> str:
        async with self._invocation(request.session_id, mode="start") as (activity_id, run_id):
            async with self._session_factory() as db, db.begin():
                session = await SessionRepository(db).claim_run(
                    request.session_id, activity_id, run_id
                )
                state = decode_state(session.runner_state)
                require_new_prompt(state)
                validate_history(
                    state, await HistoryRepository(db).read_history_entries(session.id)
                )
                data = await self._input(db, session, request.user_prompt, resuming=False)
            return await self._run(activity_id, run_id, data)

    async def _input(
        self,
        db: AsyncSession,
        session: SessionRow,
        user_prompt: str | None,
        *,
        resuming: bool,
    ) -> RunnerInput:
        """Build runtime-only configuration under the admitted session row's guard."""
        catalog = ModelRepository(db)
        model = await catalog.get_model(session.provider_id, session.model_name)
        provider = await catalog.get_provider_config(session.provider_id)
        _, model_class = resolve_classes(provider)
        settings = validate_settings(model_class, model.settings | session.model_settings)
        if resuming and session.layers.keys() != session.layer_states.keys():
            raise ValueError("Checkpoint State must match fixed layer Config")
        return RunnerInput(
            session_id=session.id,
            runner_state=session.runner_state,
            layers=layer_registry.load(session.layers, session.layer_states),
            user_prompt=user_prompt,
            resuming=resuming,
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

    async def _resume_input(
        self, session_id: UUID, activity_id: UUID, run_id: str
    ) -> RunnerInput | str:
        """Confirm an exact old owner outside the CAS/admission transaction."""
        async with self._session_factory() as db, db.begin():
            row = await SessionRepository(db)._session(session_id)
            if row.status != LifecycleStatus.READY:
                raise LifecycleError(f"Session {session_id} is {row.status}")
            previous = None
            if row.current_activity_id is not None:
                assert row.current_activity_run_id is not None
                previous = (row.current_activity_id, row.current_activity_run_id, row.status)
        if previous is not None:
            await self._confirm_terminal(previous[0], previous[1])
        async with self._session_factory() as db, db.begin():
            repo = SessionRepository(db)
            session = await repo.claim_close_or_run(
                session_id, activity_id, run_id, close=False, previous=previous
            )
            if session is None:
                raise SessionBusy("Another execution changed the session")
            state = decode_state(session.runner_state)
            if not isinstance(state, Checkpoint):
                raise ValueError("Explicit resume requires a versioned checkpoint")
            validate_history(state, await HistoryRepository(db).read_history_entries(session_id))
            if isinstance(state.next, DoneNext):
                # No execution configuration, layer registry or resources are
                # consulted for a completed business result.
                await repo.complete(
                    session_id,
                    activity_id,
                    run_id,
                    (LifecycleStatus.READY,),
                    LifecycleStatus.READY,
                )
                return state.next.output
            return await self._input(db, session, None, resuming=True)

    @activity.defn(name="kapy.resume_agent")
    async def resume(self, request: SessionActivityRequest) -> str:
        async with self._invocation(request.session_id, mode="resume") as (activity_id, run_id):
            data = await self._resume_input(request.session_id, activity_id, run_id)
            if isinstance(data, str):
                return data
            return await self._run(activity_id, run_id, data)

    async def _run(self, activity_id: UUID, run_id: str, data: RunnerInput) -> str:
        state = decode_state(data.runner_state)
        history = state_messages(state)
        provider_config = data.config.provider_config()
        provider_class, model_class = resolve_classes(provider_config)
        deps = RunnerDeps(
            session_id=data.session_id,
            activity_id=activity_id,
            activity_run_id=run_id,
            session_factory=self._session_factory,
            output_service=self._output_service,
            layers=data.layers,
            recorder=Recorder(history, resuming=data.resuming),
            resume=state if data.resuming and isinstance(state, Checkpoint) else None,
        )
        await check_owner(
            self._session_factory,
            data.session_id,
            activity_id,
            run_id,
            (LifecycleStatus.READY,),
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
            result = await fenced_agent(agent).run(
                data.user_prompt,
                model=model,
                model_settings=cast(ModelSettings, data.config.model_settings),
                deps=deps,
                message_history=history,
                event_stream_handler=handle_deltas,
            )
        await check_owner(
            self._session_factory,
            data.session_id,
            activity_id,
            run_id,
            (LifecycleStatus.READY,),
        )
        await deps.recorder.record_messages(
            deps,
            result.all_messages(),
            checkpoint="run_completed",
            next=DoneNext(output=result.output),
            release=True,
        )
        return result.output

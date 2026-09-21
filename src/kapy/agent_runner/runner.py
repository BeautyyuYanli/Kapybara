"""Resumable node execution and fenced input acceptance, with short DB transactions.

The application owns Agent, deps and the session factory. A handle belongs to the
asyncio task that opens it: the native graph owns task-local AnyIO cancel scopes.
The borrowed SessionLease renews ownership through graph cleanup. Committed
history is independent of SDK working messages and is never rewritten.
"""

import asyncio
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from copy import deepcopy
from typing import Any
from uuid import UUID

from pydantic_ai import Agent, CallToolsNode, DeferredToolRequests, ModelRequestNode, UserPromptNode
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.run import AgentRun
from pydantic_graph import End
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kapy.session_lease import SessionLease, open_session_lease

from .compaction import assemble_context, replay_start, require_nonnegative_int, summarize
from .output import OutputCapability
from .repository import AgentRepository, response_tokens
from .types import (
    Compaction,
    ConsumeCancel,
    HistoryMessage,
    InputBatch,
    MessageCommitted,
    NextStep,
    OutputCallback,
    ReadInputs,
    ResumeState,
    TurnResult,
    UserInput,
)


class AgentRunner[OutputT]:
    """One task's execution lease and native graph; obtain it using open_runner.

    Operations are sequential and non-reentrant. Any execution failure invalidates
    the handle; recovery requires opening a new one. A cancel signal is a normal
    run return and leaves the handle usable. External side effects may replay
    when they happened after the last committed checkpoint.
    """

    def __init__(
        self,
        session_id: UUID,
        lease: SessionLease,
        state: ResumeState,
        *,
        agent: Agent[Any, OutputT],
        deps: Any,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._session_id = session_id
        self._lease = lease
        self._agent = agent
        self._deps = deps
        self._session_factory = session_factory
        self._next_step = state.next_step
        self._context: list[ModelMessage] | None = None
        self._next_seq = state.next_seq
        self._output = OutputCapability(session_id)
        self._compaction = state.compaction
        self._latest_response_seq: int | None = None
        self._observed_context_tokens: int | None = None
        self._owner = asyncio.current_task()
        self._occupied = False
        self._closed = False
        self._error: BaseException | None = None
        self._native_context: AbstractAsyncContextManager[AgentRun[Any, OutputT]] | None = None
        self._native: AgentRun[Any, OutputT] | None = None
        self._node: ModelRequestNode[Any, OutputT] | CallToolsNode[Any, OutputT] | None = None

    @property
    def next_step(self) -> NextStep:
        return self._next_step

    def _ensure_usable(self) -> None:
        self._lease.check()
        if self._error is not None:
            raise self._error
        if self._closed:
            raise RuntimeError("Runner is closed")

    @contextmanager
    def _operation(self) -> Iterator[None]:
        self._ensure_usable()
        if asyncio.current_task() is not self._owner:
            raise RuntimeError("Runner must be used by the task that opened it")
        if self._occupied:
            raise RuntimeError("Runner is already executing")
        self._occupied = True
        try:
            yield
        finally:
            self._occupied = False

    async def turn(self, *, steer: Sequence[UserInput] = ()) -> TurnResult[OutputT]:
        """Advance a model response and its complete tool/output handling batch.

        At handle_response, only process the saved response without requesting it
        again. Nonempty steer in that state raises ValueError and invalidates the
        handle. At done with no steer, return finished=True and output=None without
        rebuilding an earlier output or running the model. Otherwise finished
        reflects whether this turn committed done; output is its new final result.
        Call rebuild_context first; this operation never loads history implicitly.
        """
        with self._operation():
            try:
                self._require_context()
                if steer and self._next_step == "handle_response":
                    raise ValueError("Cannot accept steer before handling the saved response")
                await self._accept_inputs(steer=steer)
                return await self._advance_turn()
            except BaseException as error:
                await self._fail(error)
                raise

    async def run(
        self,
        *,
        read_steer: ReadInputs,
        consume_cancel: ConsumeCancel,
        initial: InputBatch | None = None,
        compaction_threshold_tokens: int | None = None,
        compaction_replay_turns: int = 10,
        on_output: OutputCallback | None = None,
    ) -> TurnResult[OutputT]:
        """Prepare context and run until done with no steer, checking cancel first.

        A positive threshold enables compaction using the latest complete business
        response's input+output usage; None disables it. Replay is approximately N
        responses, extending backwards to complete tool pairs. N must be a
        nonnegative integer. A new summary never replaces the business output.
        on_output enables business text and committed-message events for this call
        only. Callback errors invalidate the handle; cancellation still propagates.
        """
        with self._operation():
            self._output.callback = on_output
            try:
                require_nonnegative_int(compaction_replay_turns, "compaction_replay_turns")
                if compaction_threshold_tokens is not None:
                    require_nonnegative_int(
                        compaction_threshold_tokens, "compaction_threshold_tokens"
                    )
                    if compaction_threshold_tokens == 0:
                        raise ValueError("compaction_threshold_tokens must be positive")
                result: TurnResult[OutputT] = TurnResult(self._next_step == "done")
                context_prepared = False
                while True:
                    self._ensure_usable()
                    async with self._session_factory.begin() as db:
                        await self._lease.lock_owned(db)
                        cancel = await consume_cancel(db)
                    self._ensure_usable()
                    if cancel:
                        return result
                    if not context_prepared:
                        await self._rebuild_context(replay_turns=compaction_replay_turns)
                        context_prepared = True
                    if self._next_step == "handle_response":
                        result = await self._advance_turn()
                        continue
                    if await self._maybe_compact(compaction_threshold_tokens):
                        await self._rebuild_context(replay_turns=compaction_replay_turns)
                        continue
                    batch = await read_steer()
                    self._ensure_usable()
                    batches = [item for item in (initial, batch) if item is not None]
                    await self._accept_inputs(batches=batches)
                    initial = None
                    if self._next_step == "done":
                        return result
                    result = await self._advance_turn()
            except BaseException as error:
                await self._fail(error)
                raise
            finally:
                self._output.callback = None

    def _require_context(self) -> list[ModelMessage]:
        if self._context is None:
            raise RuntimeError("Runner context has not been prepared; call rebuild_context first")
        return self._context

    async def rebuild_context(self, *, compaction_replay_turns: int = 10) -> None:
        """Load required history and replace context without advancing any graph node.

        Required once before manual turn/compact; run prepares it automatically.
        N=0 omits replay. Later calls apply their N to the latest saved summary.
        Reads are bounded by the committed cursor and do not hold the lease row lock.
        """
        with self._operation():
            try:
                require_nonnegative_int(compaction_replay_turns, "compaction_replay_turns")
                await self._rebuild_context(replay_turns=compaction_replay_turns)
            except BaseException as error:
                await self._fail(error)
                raise

    async def _rebuild_context(self, *, replay_turns: int) -> None:
        first = self._context is None
        if self._compaction is None and not first:
            return
        through_seq = self._next_seq - 1
        async with self._session_factory.begin() as db:
            repo = AgentRepository(db)
            compaction = self._compaction
            if compaction is None:
                tail = await repo.read_history(
                    self._session_id, start_seq=0, through_seq=through_seq
                )
                context = [message for _, message in tail]
            else:
                replay_rows: list[tuple[int, ModelMessage]] = []
                if replay_turns:
                    before = compaction.last_message_seq
                    while True:
                        page = await repo.read_history_before(
                            self._session_id, through_seq=before, limit=64
                        )
                        if not page:
                            raise RuntimeError("Compaction history is missing")
                        replay_rows[:0] = reversed(page)
                        start = replay_start(replay_rows, replay_turns)
                        if start is not None:
                            replay_rows = [(seq, msg) for seq, msg in replay_rows if seq >= start]
                            break
                        before = replay_rows[0][0] - 1
                first_rows = (
                    replay_rows[:1]
                    if replay_rows and replay_rows[0][0] == 0
                    else await repo.read_history(self._session_id, start_seq=0, through_seq=0)
                )
                system_parts = [
                    part
                    for _, message in first_rows
                    if isinstance(message, ModelRequest)
                    for part in message.parts
                    if isinstance(part, SystemPromptPart)
                ]
                tail = await repo.read_history(
                    self._session_id,
                    start_seq=compaction.last_message_seq + 1,
                    through_seq=through_seq,
                )
                context = assemble_context(
                    system_parts,
                    compaction,
                    [message for _, message in replay_rows],
                    [message for _, message in tail],
                )
        self._ensure_usable()
        if first:
            for seq, message in tail:
                if isinstance(message, ModelResponse):
                    self._observe_response(seq, message)
        self._context = deepcopy(context)
        if self._native is not None:
            if self._next_step == "model_request":
                assert isinstance(self._node, ModelRequestNode)
                assert isinstance(context[-1], ModelRequest)
                # The pending request enters SDK history only when its node runs.
                self._native.ctx.state.message_history[:] = deepcopy(context[:-1])
                self._node.request = deepcopy(context[-1])
            elif self._next_step == "handle_response":
                self._native.ctx.state.message_history[:] = deepcopy(context)

    async def compact(self, *, max_retries: int = 2) -> Compaction | None:
        """Save a temporary text summary without changing context or checkpoint.

        Requires prepared context and a model_request/done boundary. Empty history
        returns None; an already summarized prefix returns its existing summary.
        max_retries is a nonnegative extra-attempt budget. Invalid responses exhaust
        it with UnexpectedModelBehavior; all execution failures invalidate the handle.
        Apply the saved summary using rebuild_context when manually orchestrating.
        """
        with self._operation():
            try:
                return await self._compact(max_retries=max_retries)
            except BaseException as error:
                await self._fail(error)
                raise

    async def _compact(self, *, max_retries: int = 2) -> Compaction | None:
        require_nonnegative_int(max_retries, "max_retries")
        context = self._require_context()
        if self._next_step == "handle_response":
            raise ValueError("Cannot compact before handling the saved response")
        last_seq = self._next_seq - 1
        async with self._session_factory.begin() as db:
            await self._lease.lock_owned(db)
        self._ensure_usable()
        if last_seq < 0:
            return None
        if self._compaction is not None and self._compaction.last_message_seq == last_seq:
            return self._compaction
        text = await summarize(
            self._agent,
            context,
            session_id=self._session_id,
            deps=self._deps,
            max_retries=max_retries,
        )
        self._ensure_usable()
        async with self._session_factory.begin() as db:
            repo = AgentRepository(db)
            await self._lease.lock_owned(db)
            result = await repo.save_compaction(
                self._session_id, last_message_seq=last_seq, text=text
            )
        self._ensure_usable()
        self._compaction = result
        return result

    async def _maybe_compact(self, threshold: int | None) -> bool:
        if (
            threshold is None
            or self._latest_response_seq is None
            or self._observed_context_tokens is None
            or self._observed_context_tokens <= threshold
            or (
                self._compaction is not None
                and self._latest_response_seq <= self._compaction.last_message_seq
            )
        ):
            return False
        return await self._compact() is not None

    async def _open_native(self, user_prompt: UserInput | None = None) -> None:
        """Initialize only the SDK input node, never a model/tool node."""
        assert self._native is None
        context = self._agent.iter(
            user_prompt,
            message_history=deepcopy(self._require_context()),
            conversation_id=str(self._session_id),
            deps=self._deps,
            capabilities=[self._output],
        )
        native = await context.__aenter__()
        self._native_context = context
        self._native = native
        initial = native.next_node
        if not isinstance(initial, UserPromptNode):
            raise RuntimeError("Pydantic AI did not start at UserPromptNode")
        prepared = await native.next(initial)
        self._ensure_usable()
        if self._next_step == "handle_response":
            response = native.all_messages()[-1]
            if not isinstance(response, ModelResponse):
                raise RuntimeError("handle_response history must end in a response")
            # Explicitly handle the committed response, including when instructions
            # make the SDK initialization choose a new model request instead.
            self._node = CallToolsNode(response)
        elif isinstance(prepared, ModelRequestNode):
            self._node = prepared
        else:
            raise RuntimeError("Expected a prepared ModelRequestNode")

    async def _accept_inputs(
        self, *, batches: Sequence[InputBatch] = (), steer: Sequence[UserInput] = ()
    ) -> None:
        inputs = [*steer, *(content for batch in batches for content in batch.inputs)]
        created = self._native is None and self._next_step == "done"
        while inputs:
            if created:
                combined: list[UserContent] = []
                for content in inputs:
                    combined.extend([content] if isinstance(content, str) else content)
                # Dynamic prompts and SDK hooks must run outside row-lock transactions.
                await self._open_native(combined)
            elif self._native is None:
                await self._open_native()
            self._ensure_usable()
            request: ModelRequest | None = None
            entries: Sequence[HistoryMessage] = ()
            async with self._session_factory.begin() as db:
                repo = AgentRepository(db)
                await self._lease.lock_owned(db)
                accepted = list(steer)
                for batch in batches:
                    accepted.extend(await batch.consume(db))
                rebuild = created and len(accepted) < len(inputs)
                if rebuild:
                    # Undo consumption before rebuilding prompts from the smaller set.
                    # The same immutable snapshot IDs can only shrink on the next try.
                    await db.rollback()
                elif accepted:
                    parts = [UserPromptPart(deepcopy(content)) for content in accepted]
                    if created:
                        assert isinstance(self._node, ModelRequestNode)
                        request = deepcopy(self._node.request)
                        request.parts = [
                            part for part in request.parts if not isinstance(part, UserPromptPart)
                        ]
                        request.parts.extend(parts)
                    else:
                        request = ModelRequest(parts=parts)
                    entries = await repo.save_checkpoint(
                        self._session_id,
                        next_step="model_request",
                        start_seq=self._next_seq,
                        messages=[request],
                    )
            self._ensure_usable()
            if rebuild:
                await self._close_native()
                inputs = accepted
                continue
            if not accepted:
                return
            assert request is not None
            self._accept_committed("model_request", [request])
            assert isinstance(self._node, ModelRequestNode)
            if created:
                self._node.request = deepcopy(request)
            else:
                self._node.request.parts = [*self._node.request.parts, *deepcopy(request.parts)]
            await self._publish_committed(entries)
            return

    def _accept_committed(self, next_step: NextStep, messages: Sequence[ModelMessage]) -> None:
        self._require_context().extend(deepcopy(messages))
        for offset, message in enumerate(messages):
            if isinstance(message, ModelResponse):
                self._observe_response(self._next_seq + offset, message)
        self._next_seq += len(messages)
        self._next_step = next_step

    def _observe_response(self, seq: int, response: ModelResponse) -> None:
        self._latest_response_seq = seq
        tokens = response_tokens(response)
        self._observed_context_tokens = sum(tokens) if tokens is not None else None

    async def _checkpoint(self, next_step: NextStep, messages: Sequence[ModelMessage]) -> None:
        self._ensure_usable()
        async with self._session_factory.begin() as db:
            repo = AgentRepository(db)
            await self._lease.lock_owned(db)
            entries = await repo.save_checkpoint(
                self._session_id,
                next_step=next_step,
                start_seq=self._next_seq,
                messages=messages,
            )
        self._ensure_usable()
        self._accept_committed(next_step, messages)
        await self._publish_committed(entries)

    async def _publish_committed(self, entries: Sequence[HistoryMessage]) -> None:
        if self._output.callback is not None:
            for entry in entries:
                await self._output.callback(MessageCommitted(entry))

    async def _advance_turn(self) -> TurnResult[OutputT]:
        self._ensure_usable()
        if self._next_step == "done":
            return TurnResult(True)
        if self._native is None:
            await self._open_native()
        assert self._native is not None
        if self._next_step == "model_request":
            assert isinstance(self._node, ModelRequestNode)
            self._output.response_seq = self._next_seq
            try:
                following = await self._native.next(self._node)
            finally:
                self._output.response_seq = None
            self._ensure_usable()
            if not isinstance(following, CallToolsNode):
                raise RuntimeError("Expected a complete model response")
            response = deepcopy(following.model_response)
            if response.state != "complete":
                raise RuntimeError("Only complete model responses can be checkpointed")
            await self._checkpoint("handle_response", [response])
            self._node = following
        assert isinstance(self._node, CallToolsNode)
        history_start = len(self._native.all_messages())
        following = await self._native.next(self._node)
        self._ensure_usable()
        if isinstance(following, ModelRequestNode):
            await self._checkpoint("model_request", [deepcopy(following.request)])
            self._node = following
            return TurnResult(False)
        if isinstance(following, End):
            if isinstance(following.data.output, DeferredToolRequests):
                raise ValueError("Deferred tools are not supported by this runner")
            messages = deepcopy(self._native.all_messages()[history_start:])
            if any(not isinstance(message, ModelRequest) for message in messages):
                raise RuntimeError("Unexpected SDK messages during output completion")
            await self._checkpoint("done", messages)
            output = following.data.output
            await self._close_native()
            return TurnResult(True, output)
        raise RuntimeError("Unexpected response handler node")

    def _remember_error(self, error: BaseException) -> None:
        previous = self._error
        if previous is not None and previous is not error:
            error.add_note(f"Earlier runner failure: {previous!r}")
            for note in getattr(previous, "__notes__", ()):
                error.add_note(note)
        self._error = error

    async def _close_native(self, error: BaseException | None = None) -> None:
        context = self._native_context
        self._native_context = None
        self._native = None
        self._node = None
        if context is not None:
            owner = asyncio.current_task()
            assert owner is not None
            cancelling = owner.cancelling()
            await context.__aexit__(
                type(error) if error is not None else None,
                error,
                error.__traceback__ if error is not None else None,
            )
            # SDK cancel-scope cleanup can consume an asyncio cancellation sent
            # during exit. Preserve that caller signal after its scopes unwind.
            if owner.cancelling() > cancelling:
                raise asyncio.CancelledError

    async def _fail(self, error: BaseException) -> None:
        self._remember_error(error)
        try:
            await self._close_native(error)
        except BaseException as cleanup_error:
            error.add_note(f"Native run cleanup also failed: {cleanup_error!r}")

    async def _close(self) -> None:
        self._closed = True
        # Native AnyIO scopes must close in the owner task, while the outer
        # SessionLease still maintains its heartbeat.
        await self._close_native()


@asynccontextmanager
async def open_runner[DepsT, OutputT](
    session_id: UUID,
    *,
    agent: Agent[DepsT, OutputT],
    session_factory: async_sessionmaker[AsyncSession],
    deps: DepsT = None,
    heartbeat_interval: float = 10.0,
    heartbeat_timeout: float = 60.0,
) -> AsyncIterator[AgentRunner[OutputT]]:
    """Acquire metadata, heartbeat and release one execution lease in the calling task.

    History is loaded by rebuild_context or run after this context yields.
    Use the Agent's default max_concurrency=None: Agent-level limits do not support
    the nested compaction graph. Configure ConcurrencyLimitedModel on the model or
    limit workers outside start_runner instead; the runner never overrides limits.

    Heartbeat values are seconds and must be finite with
    0 < heartbeat_interval < heartbeat_timeout, otherwise ValueError is raised.
    Timeout only permits takeover: it neither limits a turn nor revokes a token
    until another operation following the lease protocol acquires ownership.
    A live owner prevents acquisition with
    SessionBusy; replaced ownership raises RunnerLost at a subsequent check.

    Database exceptions propagate unchanged. Background heartbeat failures surface
    at the next execution boundary or context exit, without automatically stopping
    external work or retrying. The handle is unusable after execution failure;
    recovery requires a new open_runner context.
    """
    async with open_session_lease(
        session_id,
        session_factory=session_factory,
        heartbeat_interval=heartbeat_interval,
        heartbeat_timeout=heartbeat_timeout,
    ) as lease:
        async with session_factory.begin() as db:
            await lease.lock_owned(db)
            state = await AgentRepository(db).resume(session_id)
        runner = AgentRunner(
            session_id,
            lease,
            state,
            agent=agent,
            deps=deps,
            session_factory=session_factory,
        )
        try:
            yield runner
            runner._ensure_usable()
        except BaseException as error:
            runner._remember_error(error)
            raise
        finally:
            try:
                await runner._close()
            except BaseException as error:
                runner._remember_error(error)
                raise


async def start_runner[DepsT, OutputT](
    session_id: UUID,
    *,
    agent: Agent[DepsT, OutputT],
    session_factory: async_sessionmaker[AsyncSession],
    read_steer: ReadInputs,
    read_queued: ReadInputs,
    consume_cancel: ConsumeCancel,
    deps: DepsT = None,
    heartbeat_interval: float = 10.0,
    heartbeat_timeout: float = 60.0,
    compaction_threshold_tokens: int | None = None,
    compaction_replay_turns: int = 10,
    on_output: OutputCallback | None = None,
) -> TurnResult[OutputT]:
    """Drain queued snapshots after each run, preserving the last produced output.

    An empty or fully withdrawn later snapshot still supplies the latest finished
    state, but does not erase an output already produced during this call.
    """
    async with open_runner(
        session_id,
        agent=agent,
        session_factory=session_factory,
        deps=deps,
        heartbeat_interval=heartbeat_interval,
        heartbeat_timeout=heartbeat_timeout,
    ) as runner:
        initial = None
        output: OutputT | None = None
        while True:
            result = await runner.run(
                initial=initial,
                read_steer=read_steer,
                consume_cancel=consume_cancel,
                compaction_threshold_tokens=compaction_threshold_tokens,
                compaction_replay_turns=compaction_replay_turns,
                on_output=on_output,
            )
            runner._ensure_usable()
            if result.output is not None:
                output = result.output
            initial = await read_queued()
            runner._ensure_usable()
            if initial is None:
                return TurnResult(result.finished, output)

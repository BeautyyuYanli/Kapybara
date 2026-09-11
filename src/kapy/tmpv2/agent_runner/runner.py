"""Resumable node execution and fenced input acceptance, with short DB transactions.

The application owns Agent, deps and the session factory. A handle belongs to the
asyncio task that opens it: the native graph owns task-local AnyIO cancel scopes.
Only the heartbeat runs separately, always with its own AsyncSession. Committed
history is independent of SDK working messages and is never rewritten.
"""

import asyncio
import math
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from pydantic_ai import Agent, CallToolsNode, DeferredToolRequests, ModelRequestNode, UserPromptNode
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    UserContent,
    UserPromptPart,
)
from pydantic_ai.run import AgentRun
from pydantic_graph import End
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .repository import AgentRepository
from .types import (
    ConsumeCancel,
    InputBatch,
    NextStep,
    ReadInputs,
    ResumeState,
    TurnResult,
    UserInput,
)


class AgentRunner[OutputT]:
    """One task's execution lease and native graph; obtain it using open_runner.

    turn/run are sequential and non-reentrant. Any execution failure invalidates
    the handle; recovery requires opening a new one. A cancel signal is a normal
    run return and leaves the handle usable. External side effects may replay
    when they happened after the last committed checkpoint.
    """

    def __init__(
        self,
        session_id: UUID,
        lock_token: UUID,
        state: ResumeState,
        *,
        agent: Agent[Any, OutputT],
        deps: Any,
        session_factory: async_sessionmaker[AsyncSession],
        heartbeat_interval: float,
    ) -> None:
        self._session_id = session_id
        self._lock_token = lock_token
        self._agent = agent
        self._deps = deps
        self._session_factory = session_factory
        self._heartbeat_interval = heartbeat_interval
        self._next_step = state.next_step
        self._history = list(state.history)
        self._next_seq = len(state.history)
        self._owner = asyncio.current_task()
        self._occupied = False
        self._closed = False
        self._error: BaseException | None = None
        self._native_context: AbstractAsyncContextManager[AgentRun[Any, OutputT]] | None = None
        self._native: AgentRun[Any, OutputT] | None = None
        self._node: ModelRequestNode[Any, OutputT] | CallToolsNode[Any, OutputT] | None = None
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(), name=f"agent-heartbeat:{session_id}"
        )

    @property
    def next_step(self) -> NextStep:
        return self._next_step

    def _ensure_usable(self) -> None:
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
        """
        with self._operation():
            try:
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
    ) -> TurnResult[OutputT]:
        """Check cancel and accept steer at turn boundaries until done with no input."""
        with self._operation():
            try:
                result: TurnResult[OutputT] = TurnResult(self._next_step == "done")
                while True:
                    self._ensure_usable()
                    async with self._session_factory.begin() as db:
                        await AgentRepository(db).lock_owned(self._session_id, self._lock_token)
                        cancel = await consume_cancel(db)
                    self._ensure_usable()
                    if cancel:
                        return result
                    if self._next_step != "handle_response":
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

    async def _open_native(self, user_prompt: UserInput | None = None) -> None:
        """Initialize only the SDK input node, never a model/tool node."""
        assert self._native is None
        context = self._agent.iter(
            user_prompt,
            message_history=deepcopy(self._history),
            conversation_id=str(self._session_id),
            deps=self._deps,
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
        inputs = list(steer)
        for batch in batches:
            inputs.extend(batch.inputs)
        if not inputs:
            return
        parts = [UserPromptPart(deepcopy(content)) for content in inputs]
        created = self._native is None and self._next_step == "done"
        if created:
            combined: list[UserContent] = []
            for content in inputs:
                combined.extend([content] if isinstance(content, str) else content)
            await self._open_native(combined)
            assert isinstance(self._node, ModelRequestNode)
            # The preparation node supplies initial system parts. Persist one
            # UserPromptPart per input rather than the temporary flattened prompt.
            request = deepcopy(self._node.request)
            request.parts = [part for part in request.parts if not isinstance(part, UserPromptPart)]
            request.parts.extend(parts)
            self._node.request = deepcopy(request)
        else:
            if self._native is None:
                await self._open_native()
            request = ModelRequest(parts=parts)
        self._ensure_usable()
        async with self._session_factory.begin() as db:
            repo = AgentRepository(db)
            await repo.lock_owned(self._session_id, self._lock_token)
            for batch in batches:
                await batch.consume(db)
            await repo.save_checkpoint(
                self._session_id,
                next_step="model_request",
                start_seq=self._next_seq,
                messages=[request],
            )
        self._ensure_usable()
        self._accept_committed("model_request", [request])
        if not created:
            assert isinstance(self._node, ModelRequestNode)
            self._node.request.parts = [*self._node.request.parts, *deepcopy(request.parts)]

    def _accept_committed(self, next_step: NextStep, messages: Sequence[ModelMessage]) -> None:
        self._history.extend(deepcopy(messages))
        self._next_seq += len(messages)
        self._next_step = next_step

    async def _checkpoint(self, next_step: NextStep, messages: Sequence[ModelMessage]) -> None:
        self._ensure_usable()
        async with self._session_factory.begin() as db:
            repo = AgentRepository(db)
            await repo.lock_owned(self._session_id, self._lock_token)
            await repo.save_checkpoint(
                self._session_id,
                next_step=next_step,
                start_seq=self._next_seq,
                messages=messages,
            )
        self._ensure_usable()
        self._accept_committed(next_step, messages)

    async def _advance_turn(self) -> TurnResult[OutputT]:
        self._ensure_usable()
        if self._next_step == "done":
            return TurnResult(True)
        if self._native is None:
            await self._open_native()
        assert self._native is not None
        if self._next_step == "model_request":
            assert isinstance(self._node, ModelRequestNode)
            following = await self._native.next(self._node)
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

    async def _heartbeat(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval)
                async with self._session_factory.begin() as db:
                    await AgentRepository(db).heartbeat(self._session_id, self._lock_token)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # Do not cancel external tool/model work. Every subsequent boundary
            # observes this failure, and no later checkpoint can be accepted.
            self._remember_error(error)

    def _remember_error(self, error: BaseException) -> None:
        previous = self._error
        if previous is not None and previous is not error:
            error.add_note(f"Earlier runner failure: {previous!r}")
            for note in getattr(previous, "__notes__", ()):
                error.add_note(note)
        self._error = error

    async def _stop_heartbeat(self) -> None:
        self._heartbeat_task.cancel()
        # gather turns only the child's cancellation into a result. Cancellation
        # of the calling task still raises and must survive resource cleanup.
        await asyncio.gather(self._heartbeat_task, return_exceptions=True)

    async def _close_native(self, error: BaseException | None = None) -> None:
        context = self._native_context
        self._native_context = None
        self._native = None
        self._node = None
        if context is not None:
            await context.__aexit__(
                type(error) if error is not None else None,
                error,
                error.__traceback__ if error is not None else None,
            )

    async def _fail(self, error: BaseException) -> None:
        self._remember_error(error)
        await self._stop_heartbeat()
        try:
            await self._close_native(error)
        except BaseException as cleanup_error:
            error.add_note(f"Native run cleanup also failed: {cleanup_error!r}")

    async def _close(self) -> None:
        self._closed = True
        try:
            await self._stop_heartbeat()
        finally:
            try:
                await self._close_native()
            finally:
                async with self._session_factory.begin() as db:
                    await AgentRepository(db).release(self._session_id, self._lock_token)


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
    """Acquire, load, heartbeat and release one execution lease in the calling task.

    Heartbeat values are seconds and must be finite with
    0 < heartbeat_interval < heartbeat_timeout, otherwise ValueError is raised.
    Timeout only permits takeover: it neither limits a turn nor revokes a token
    until another runner acquires it. A live owner prevents acquisition with
    SessionBusy; replaced ownership raises RunnerLost at a subsequent check.

    Database exceptions propagate unchanged. Background heartbeat failures surface
    at the next execution boundary or context exit, without automatically stopping
    external work or retrying. The handle is unusable after execution failure;
    recovery requires a new open_runner context.
    """
    if not (
        math.isfinite(heartbeat_interval)
        and math.isfinite(heartbeat_timeout)
        and 0 < heartbeat_interval < heartbeat_timeout
    ):
        raise ValueError("Require finite 0 < heartbeat_interval < heartbeat_timeout")
    token = uuid4()
    async with session_factory.begin() as db:
        state = await AgentRepository(db).acquire(
            session_id, token, heartbeat_timeout=heartbeat_timeout
        )
    runner = AgentRunner(
        session_id,
        token,
        state,
        agent=agent,
        deps=deps,
        session_factory=session_factory,
        heartbeat_interval=heartbeat_interval,
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
) -> TurnResult[OutputT]:
    """Run first, then start another run for each queued snapshot, including after cancel."""
    async with open_runner(
        session_id,
        agent=agent,
        session_factory=session_factory,
        deps=deps,
        heartbeat_interval=heartbeat_interval,
        heartbeat_timeout=heartbeat_timeout,
    ) as runner:
        initial = None
        while True:
            result = await runner.run(
                initial=initial, read_steer=read_steer, consume_cancel=consume_cancel
            )
            runner._ensure_usable()
            initial = await read_queued()
            runner._ensure_usable()
            if initial is None:
                return result

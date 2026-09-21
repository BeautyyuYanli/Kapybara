"""Lease ownership, sequential scheduling and thin public SDK graph driving.

A runner belongs to its opening task because Agent.iter owns task-local AnyIO
scopes. ExecutionState and its capability own the durable node protocol; this
module never mutates SDK history or duplicates its node transitions. Shared
Agent, deps and database resources remain application-owned.
"""

import asyncio
import math
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from copy import deepcopy
from typing import Any
from uuid import UUID, uuid4

from pydantic_ai import Agent
from pydantic_ai.messages import UserContent
from pydantic_ai.run import AgentRun
from pydantic_graph import End
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .context import ContextPolicy, full_history_policy
from .execution import ExecutionState, InputPreparation, SessionExecutionCapability
from .output import OutputCapability
from .repository import AgentRepository
from .types import (
    ConsumeCancel,
    ContextPage,
    InputBatch,
    NextStep,
    OutputCallback,
    ReadInputs,
    ResumeState,
    TurnResult,
    UserInput,
)


class AgentRunner[OutputT]:
    """One task's lease and native graph; obtain it using open_runner.

    Operations are sequential and non-reentrant. Execution failure invalidates the
    handle; user cancel is a normal return preserving the committed checkpoint.
    The context policy is fixed for the handle's entire lifetime.
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
        context_policy: ContextPolicy | None,
    ) -> None:
        self._session_id, self._lock_token = session_id, lock_token
        self._agent, self._deps = agent, deps
        self._session_factory, self._heartbeat_interval = session_factory, heartbeat_interval
        self._output = OutputCapability(session_id)
        self._execution = ExecutionState(
            session_id,
            lock_token,
            state,
            session_factory=session_factory,
            policy=context_policy if context_policy is not None else full_history_policy(),
            output=self._output,
        )
        self._owner = asyncio.current_task()
        self._occupied = False
        self._closed = False
        self._native_context: AbstractAsyncContextManager[AgentRun[Any, OutputT]] | None = None
        self._native: AgentRun[Any, OutputT] | None = None
        self._capability: SessionExecutionCapability | None = None
        self._heartbeat_task = asyncio.create_task(
            self._heartbeat(), name=f"agent-heartbeat:{session_id}"
        )

    @property
    def next_step(self) -> NextStep:
        return self._execution.next_step

    def _ensure_usable(self) -> None:
        self._execution.ensure_usable()
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
        """Advance one response and its complete tool/output batch, without auto paging.

        Call rebuild_context first. A saved handle_response resumes without another
        model call and rejects steer. Done with no steer produces no historical output.
        """
        with self._operation():
            try:
                self._execution.require_context()
                if steer and self.next_step == "handle_response":
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
        on_output: OutputCallback | None = None,
    ) -> TurnResult[OutputT]:
        """Prepare context and run until done with no steer, checking cancel first.

        Safe boundaries apply the injected policy. Page actions never replace the
        business result. The output callback belongs only to this call; callback
        errors invalidate the handle. Queued input is drained by start_runner.
        """
        with self._operation():
            execution = self._execution
            result: TurnResult[OutputT] = TurnResult(self.next_step == "done")
            prepared = False
            self._output.callback = on_output
            try:
                while True:
                    self._ensure_usable()
                    if await execution.consume_cancel(consume_cancel):
                        return result
                    if not prepared:
                        await execution.rebuild_context()
                        prepared = True
                    if self.next_step == "handle_response":
                        result = await self._advance_turn()
                        continue
                    if await execution.maybe_turn_context_page():
                        continue
                    batch = await read_steer()
                    self._ensure_usable()
                    await self._accept_inputs(
                        batches=[b for b in (initial, batch) if b is not None]
                    )
                    initial = None
                    if self.next_step == "done":
                        return result
                    result = await self._advance_turn()
            except BaseException as error:
                await self._fail(error)
                raise
            finally:
                self._output.callback = None

    async def rebuild_context(self) -> None:
        """Reassemble committed page state, without rerunning its action or a graph node."""
        with self._operation():
            try:
                await self._execution.rebuild_context()
            except BaseException as error:
                await self._fail(error)
                raise

    async def turn_context_page(self) -> ContextPage | None:
        """Force a safe-boundary page action and apply its view; same anchors reuse state.

        Requires prepared context. Actions run outside transactions with heartbeat
        alive; failures invalidate the handle. Reopening reuses any committed page.
        """
        with self._operation():
            try:
                return await self._execution.turn_context_page()
            except BaseException as error:
                await self._fail(error)
                raise

    async def _open_native(self, preparation: InputPreparation | None = None) -> None:
        assert self._native is None
        prompt: list[UserContent] | None = None
        if preparation is not None:
            prompt = []
            for item in preparation.candidates:
                prompt.extend([item] if isinstance(item, str) else item)
        capability = SessionExecutionCapability(self._execution, preparation)
        context = self._agent.iter(
            prompt,
            message_history=deepcopy(self._execution.require_context()),
            conversation_id=str(self._session_id),
            deps=self._deps,
            capabilities=[capability, self._output],
        )
        native = await context.__aenter__()
        self._native_context, self._native, self._capability = context, native, capability
        capability.read_messages = native.all_messages
        initial = native.next_node
        if isinstance(initial, End):
            raise RuntimeError("Pydantic AI ended before preparation")
        await native.next(initial)
        self._ensure_usable()

    async def _accept_inputs(
        self,
        *,
        batches: Sequence[InputBatch] = (),
        steer: Sequence[UserInput] = (),
    ) -> None:
        inputs = tuple([*steer, *(content for batch in batches for content in batch.inputs)])
        created = self._native is None and self.next_step == "done"
        while inputs:
            preparation = InputPreparation(inputs, batches, steer)
            if created:
                await self._open_native(preparation)
                assert self._capability is not None
                retry = self._capability.retry_inputs
                if retry is not None:
                    await self._close_native()
                    inputs = retry
                    continue
            else:
                if self._native is None:
                    await self._open_native()
                assert self._native is not None and self._capability is not None
                node = self._native.next_node
                if isinstance(node, End):
                    raise RuntimeError("Cannot accept input into a completed SDK run")
                await self._capability.accept_inputs(node, preparation)
            self._ensure_usable()
            return

    async def _advance_turn(self) -> TurnResult[OutputT]:
        self._ensure_usable()
        if self.next_step == "done":
            return TurnResult(True)
        if self._native is None:
            await self._open_native()
        assert self._native is not None
        while True:
            previous = self.next_step
            node = self._native.next_node
            if isinstance(node, End):
                raise RuntimeError("SDK ended before the durable execution checkpoint")
            await self._native.next(node)
            self._ensure_usable()
            if previous == "handle_response":
                result = self._execution.last_turn_result
                if result.finished:
                    await self._close_native()
                return result

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
        self._execution.remember_error(error)

    async def _stop_heartbeat(self) -> None:
        self._heartbeat_task.cancel()
        # gather turns only the child's cancellation into a result. Cancellation
        # of the calling task still raises and must survive resource cleanup.
        await asyncio.gather(self._heartbeat_task, return_exceptions=True)

    async def _close_native(self, error: BaseException | None = None) -> None:
        context = self._native_context
        self._native_context = None
        self._native = None
        self._capability = None
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
    context_policy: ContextPolicy | None = None,
    heartbeat_interval: float = 10.0,
    heartbeat_timeout: float = 60.0,
) -> AsyncIterator[AgentRunner[OutputT]]:
    """Acquire metadata, heartbeat and release one execution lease in the calling task.

    History is loaded by rebuild_context or run after this context yields.
    Use the Agent's default max_concurrency=None: Agent-level limits do not support
    nested page-action graphs. Configure ConcurrencyLimitedModel on the model or
    limit workers outside start_runner instead; the runner never overrides limits.

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
        context_policy=context_policy,
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
    context_policy: ContextPolicy | None = None,
    heartbeat_interval: float = 10.0,
    heartbeat_timeout: float = 60.0,
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
        context_policy=context_policy,
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
                on_output=on_output,
            )
            runner._ensure_usable()
            if result.output is not None:
                output = result.output
            initial = await read_queued()
            runner._ensure_usable()
            if initial is None:
                return TurnResult(result.finished, output)

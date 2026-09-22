"""Durable execution protocol driven by public Pydantic AI capability hooks.

ExecutionState owns committed copies and absolute cursors, borrowing resources.
Only successful fenced transactions advance them. Mutating transactions lock the
borrowed SessionLease before business rows, keeping that check and writes together.
The per-SDK-run capability owns preparation/node observations, never a lease or a
shared Agent's deps.
"""

import json
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any
from uuid import UUID

from pydantic import TypeAdapter
from pydantic_ai import (
    CallToolsNode,
    DeferredToolRequests,
    ModelRequestNode,
    RunContext,
    UserPromptNode,
)
from pydantic_ai.capabilities import (
    AbstractCapability,
    AgentNode,
    CapabilityOrdering,
    NodeResult,
    WrapNodeRunHandler,
)
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import ModelRequestContext
from pydantic_graph import End
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kapy.session_lease import SessionLease

from .context import (
    ContextPage,
    ContextPlugin,
    JsonObject,
    PageInput,
    assemble_working_context,
    bounded_history,
    protected_suffix,
    require_nonnegative_int,
)
from .output import OutputCapability
from .repository import AgentRepository, response_tokens
from .types import (
    ConsumeCancel,
    ContextPageRecord,
    HistoryMessage,
    InputBatch,
    MessageCommitted,
    NextStep,
    ResumeState,
    TurnResult,
    UserInput,
)


@dataclass(frozen=True, slots=True)
class InputPreparation:
    """Immutable candidate contents; consumption callbacks retain the original IDs."""

    candidates: tuple[UserInput, ...]
    batches: Sequence[InputBatch]
    steer: Sequence[UserInput]


class ExecutionState:
    """One lease's committed working view and failure latch; no resource ownership."""

    def __init__(
        self,
        session_id: UUID,
        lease: SessionLease,
        resume: ResumeState,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        plugin: ContextPlugin | None,
        threshold_tokens: int | None,
        replay_turns: int,
        output: OutputCapability,
    ) -> None:
        self.session_id, self.lease = session_id, lease
        require_nonnegative_int(replay_turns, "replay_turns")
        if threshold_tokens is not None:
            require_nonnegative_int(threshold_tokens, "threshold_tokens")
            if threshold_tokens == 0:
                raise ValueError("threshold_tokens must be positive")
        if plugin is not None and (not isinstance(plugin.key, str) or not plugin.key.strip()):
            raise ValueError("Context plugin key must be nonempty")
        if plugin is None and threshold_tokens is not None:
            raise ValueError("A paging threshold requires a context plugin")
        self.session_factory, self.plugin, self.output = session_factory, plugin, output
        self.threshold_tokens, self.replay_turns = threshold_tokens, replay_turns
        self.next_step, self.next_seq, self.page = resume.next_step, resume.next_seq, resume.page
        self.context: list[ModelMessage] | None = None
        self.context_changed = False
        self.latest_response_seq: int | None = None
        self.observed_tokens: tuple[int, int] | None = None
        self.error: BaseException | None = None
        self.last_turn_result: TurnResult[Any] = TurnResult(resume.next_step == "done")

    def ensure_usable(self) -> None:
        self.lease.check()
        if self.error is not None:
            raise self.error

    def remember_error(self, error: BaseException) -> None:
        previous = self.error
        if previous is not None and previous is not error:
            error.add_note(f"Earlier runner failure: {previous!r}")
            for note in getattr(previous, "__notes__", ()):
                error.add_note(note)
        self.error = error

    def require_context(self) -> list[ModelMessage]:
        if self.context is None:
            raise RuntimeError("Runner context has not been prepared; call rebuild_context first")
        return self.context

    async def consume_cancel(self, consume: ConsumeCancel) -> bool:
        self.ensure_usable()
        async with self.session_factory.begin() as db:
            await self.lease.lock_owned(db)
            cancel = await consume(db)
        self.ensure_usable()
        return cancel

    def observe_response(self, seq: int, response: ModelResponse) -> None:
        self.latest_response_seq = seq
        self.observed_tokens = response_tokens(response)

    def accept_committed(self, next_step: NextStep, messages: Sequence[ModelMessage]) -> None:
        self.require_context().extend(deepcopy(messages))
        for offset, message in enumerate(messages):
            if isinstance(message, ModelResponse):
                self.observe_response(self.next_seq + offset, message)
        self.next_seq += len(messages)
        self.next_step = next_step

    async def publish_committed(self, entries: Sequence[HistoryMessage]) -> None:
        if self.output.callback is not None:
            for entry in entries:
                await self.output.callback(MessageCommitted(entry))

    async def checkpoint(self, next_step: NextStep, messages: Sequence[ModelMessage]) -> None:
        self.ensure_usable()
        async with self.session_factory.begin() as db:
            repo = AgentRepository(db)
            await self.lease.lock_owned(db)
            entries = await repo.save_checkpoint(
                self.session_id,
                next_step=next_step,
                start_seq=self.next_seq,
                messages=messages,
            )
        self.ensure_usable()
        self.accept_committed(next_step, messages)
        await self.publish_committed(entries)

    async def rebuild_context(self) -> None:
        first = self.context is None
        context = await assemble_working_context(
            session_id=self.session_id,
            session_factory=self.session_factory,
            next_step=self.next_step,
            next_seq=self.next_seq,
            page=self.page,
            plugin=self.plugin,
            replay_turns=self.replay_turns,
        )
        if first:
            async with self.session_factory.begin() as db:
                latest = await AgentRepository(db).read_latest_response(
                    self.session_id, through_seq=self.next_seq - 1
                )
            if latest is not None:
                assert isinstance(latest.message, ModelResponse)
                self.observe_response(latest.seq, latest.message)
        self.ensure_usable()
        self.context = context
        self.context_changed = True

    async def page_anchor(self) -> int:
        """Keep the pending checkpoint and its tool pairs in the current page."""
        _, before = bounded_history(self.session_factory, self.session_id, self.next_seq - 1)
        suffix = await protected_suffix(before, next_step=self.next_step, next_seq=self.next_seq)
        return suffix[0][0] - 1 if suffix else self.next_seq - 1

    async def turn_context_page(
        self, on_page: Callable[[ContextPlugin, PageInput], Awaitable[ContextPage]]
    ) -> ContextPageRecord | None:
        self.require_context()
        if self.next_step == "handle_response":
            raise ValueError("Cannot turn context page before handling the saved response")
        if self.plugin is None:
            return None
        anchor = await self.page_anchor()
        async with self.session_factory.begin() as db:
            await self.lease.lock_owned(db)
        self.ensure_usable()
        if anchor < 0:
            return None
        if self.page is not None and self.page.policy_key != self.plugin.key:
            raise ValueError("Context page policy does not match the context plugin")
        if self.page is None or self.page.anchor_seq < anchor:
            read, _ = bounded_history(self.session_factory, self.session_id, anchor)
            rows = await read(start_seq=0 if self.page is None else self.page.anchor_seq + 1)
            page_input = PageInput(
                ContextPage(deepcopy(self.page.payload)) if self.page is not None else None,
                [message for _, message in rows],
            )
            result = await on_page(self.plugin, page_input)
            # Validate without coercion, then sever mutable callback references.
            # NaN/Infinity are not portable durable JSON values.
            payload = TypeAdapter(JsonObject).validate_python(result.payload, strict=True)
            payload = json.loads(json.dumps(payload, allow_nan=False))
            self.ensure_usable()
            page = ContextPageRecord(anchor, self.plugin.key, payload)
            async with self.session_factory.begin() as db:
                await self.lease.lock_owned(db)
                await AgentRepository(db).save_page(self.session_id, page)
            self.ensure_usable()
            self.page = page
        await self.rebuild_context()
        return deepcopy(self.page)

    async def should_turn_context_page(self) -> bool:
        previous = self.page.anchor_seq if self.page is not None else -1
        if (
            self.plugin is None
            or self.threshold_tokens is None
            or self.latest_response_seq is None
            or self.observed_tokens is None
            or self.latest_response_seq <= previous
            or sum(self.observed_tokens) <= self.threshold_tokens
        ):
            return False
        # A tool checkpoint can leave the safe anchor behind the latest response.
        # Require progress so repeated boundary checks cannot repage the same prefix.
        return await self.page_anchor() > previous


class SessionExecutionCapability(AbstractCapability[Any]):
    """Per-graph adapter. after_node_run fences the final business hook result.

    Preparation withdrawal is a normal signal to the driver, never an SDK error
    for another capability to recover. Protocol/transaction failures are latched
    independently of SDK error recovery and checked again by the driver.
    """

    def __init__(self, state: ExecutionState, preparation: InputPreparation | None = None) -> None:
        self.state = state
        self.preparation = preparation
        self.retry_inputs: tuple[UserInput, ...] | None = None
        self.pending_history_count = 1
        self._handling_history: list[ModelMessage] = []
        self._request_prefix: list[ModelMessage] = []
        self._initialized = False
        self.read_messages: Callable[[], list[ModelMessage]] | None = None

    def get_ordering(self) -> CapabilityOrdering:
        # A tier alone preserves insertion order among other outermost hooks.
        # Wrap every business capability so its final node result precedes commit.
        return CapabilityOrdering(position="outermost", wraps=[AbstractCapability])

    async def wrap_node_run(
        self,
        ctx: RunContext[Any],
        *,
        node: AgentNode[Any],
        handler: WrapNodeRunHandler[Any],
    ) -> NodeResult[Any]:
        state = self.state
        try:
            state.ensure_usable()
            if isinstance(node, UserPromptNode):
                if self._initialized:
                    raise RuntimeError("Execution cannot return to input preparation")
            elif isinstance(node, ModelRequestNode) and state.next_step == "model_request":
                state.output.response_seq = state.next_seq
            elif isinstance(node, CallToolsNode) and state.next_step == "handle_response":
                self._handling_history = deepcopy(ctx.messages)
            else:
                raise RuntimeError("SDK node does not match the durable execution checkpoint")
            return await handler(node)
        except BaseException as error:
            state.remember_error(error)
            raise
        finally:
            state.output.response_seq = None

    async def after_node_run(
        self,
        ctx: RunContext[Any],
        *,
        node: AgentNode[Any],
        result: NodeResult[Any],
    ) -> NodeResult[Any]:
        state = self.state
        try:
            state.ensure_usable()
            if isinstance(node, UserPromptNode):
                self._initialized = True
                # A new graph received the assembled view through message_history.
                # SDK initialization may merge consecutive requests; applying raw
                # request boundaries again would duplicate their pending content.
                state.context_changed = False
                if state.next_step == "handle_response":
                    # UserPrompt replaces its history list; its pre-node RunContext
                    # can still refer to the old one. SDK tool provenance additionally
                    # requires response identity in the initialized graph's history.
                    assert self.read_messages is not None
                    response = self.read_messages()[-1]
                    if not isinstance(response, ModelResponse):
                        raise RuntimeError("handle_response history must end in a response")
                    return CallToolsNode(response)
                if not isinstance(result, ModelRequestNode):
                    raise RuntimeError("Expected a prepared ModelRequestNode")
                if self.preparation is not None:
                    self.retry_inputs = await self.accept_inputs(
                        result, self.preparation, created=True
                    )
            elif isinstance(node, ModelRequestNode):
                if not isinstance(result, CallToolsNode):
                    raise RuntimeError("Expected a complete model response")
                assert self.read_messages is not None
                messages = self.read_messages()
                if not messages or messages[-1] is not result.model_response:
                    raise RuntimeError("Model response must be the final SDK history message")
                if messages[:-2] != self._request_prefix:
                    raise RuntimeError("Hooks must not rewrite existing history")
                response = deepcopy(result.model_response)
                if response.state != "complete":
                    raise RuntimeError("Only complete model responses can be checkpointed")
                await state.checkpoint("handle_response", [response])
            elif isinstance(node, CallToolsNode):
                count = len(self._handling_history)
                if ctx.messages[:count] != self._handling_history:
                    raise RuntimeError("Hooks must not rewrite existing history")
                if isinstance(result, ModelRequestNode):
                    await state.checkpoint("model_request", [deepcopy(result.request)])
                    self.pending_history_count = 1
                    state.last_turn_result = TurnResult(False)
                elif isinstance(result, End):
                    if isinstance(result.data.output, DeferredToolRequests):
                        raise ValueError("Deferred tools are not supported by this runner")
                    messages = deepcopy(ctx.messages[count:])
                    if any(not isinstance(message, ModelRequest) for message in messages):
                        raise RuntimeError("Unexpected SDK messages during output completion")
                    await state.checkpoint("done", messages)
                    state.last_turn_result = TurnResult(True, result.data.output)
                else:
                    raise RuntimeError("Unexpected response handler node")
            else:
                raise RuntimeError("Unexpected execution node")
            return result
        except BaseException as error:
            state.remember_error(error)
            raise

    async def accept_inputs(
        self,
        node: AgentNode[Any],
        preparation: InputPreparation,
        *,
        created: bool = False,
    ) -> tuple[UserInput, ...] | None:
        """Accept a fixed snapshot, or return a strictly smaller preparation retry."""
        if not isinstance(node, ModelRequestNode):
            raise RuntimeError("Inputs require a pending model request")
        state = self.state
        state.ensure_usable()
        request: ModelRequest | None = None
        entries: Sequence[HistoryMessage] = ()
        async with state.session_factory.begin() as db:
            repo = AgentRepository(db)
            await state.lease.lock_owned(db)
            accepted = list(preparation.steer)
            for batch in preparation.batches:
                accepted.extend(await batch.consume(db))
            if len(accepted) > len(preparation.candidates):
                raise RuntimeError("Input consumption must not expand its candidate snapshot")
            rebuild = created and len(accepted) < len(preparation.candidates)
            if rebuild:
                await db.rollback()
            elif accepted:
                parts = [UserPromptPart(deepcopy(content)) for content in accepted]
                request = deepcopy(node.request) if created else ModelRequest(parts=[])
                request.parts = [
                    part for part in request.parts if not isinstance(part, UserPromptPart)
                ]
                request.parts.extend(parts)
                entries = await repo.save_checkpoint(
                    state.session_id,
                    next_step="model_request",
                    start_seq=state.next_seq,
                    messages=[request],
                )
        state.ensure_usable()
        if rebuild:
            return tuple(accepted)
        if request is not None:
            state.accept_committed("model_request", [request])
            if created:
                node.request = deepcopy(request)
            else:
                node.request.parts = [*node.request.parts, *deepcopy(request.parts)]
                self.pending_history_count += 1
            await state.publish_committed(entries)
        return None

    async def before_model_request(
        self,
        ctx: RunContext[Any],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        state = self.state
        try:
            state.ensure_usable()
            if state.context_changed:
                pending = request_context.messages[-1]
                if not isinstance(pending, ModelRequest):
                    raise RuntimeError("Model context must end in the pending request")
                # The SDK has already resolved this request's dynamic instructions,
                # metadata and pending content. Replace only its historical prefix.
                prefix = state.require_context()[: -self.pending_history_count]
                request_context = replace(request_context, messages=[*deepcopy(prefix), pending])
                state.context_changed = False
            self._request_prefix = deepcopy(request_context.messages[:-1])
            return request_context
        except BaseException as error:
            state.remember_error(error)
            raise

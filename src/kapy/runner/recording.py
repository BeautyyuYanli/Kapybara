"""Freeze complete messages, State and the next graph action in one transaction.

Each execution owns its Recorder. SDK working messages are a separate sending
view: request preparation may refresh framework fields, never the sealed prefix.
No graph advancement follows a failed or uncertain commit; saves are not retried.
"""

import logging
from collections.abc import Sequence
from copy import deepcopy
from typing import TYPE_CHECKING

from pydantic_ai import (
    CallToolsNode,
    ModelRequestNode,
    RunContext,
    UserPromptNode,
    capture_run_messages,
)
from pydantic_ai.capabilities import (
    AbstractCapability,
    AgentNode,
    CapabilityOrdering,
    NodeResult,
    WrapNodeRunHandler,
    WrapRunHandler,
)
from pydantic_ai.messages import InstructionPart, ModelMessage, ModelRequest
from pydantic_ai.models import ModelRequestContext
from pydantic_graph import End

from kapy.agent_history import HistoryMessage, HistoryRepository
from kapy.agent_output import MessageCommitted
from kapy.control.sessions.repository import SessionRepository
from kapy.layers.registry import layer_registry
from kapy.lifecycle import LifecycleStatus

from .checkpoints import (
    CallToolsNext,
    Checkpoint,
    CheckpointKind,
    FinishNext,
    ModelRequestNext,
    Next,
    marked_prefix,
    message_boundary,
)

if TYPE_CHECKING:
    from .types import RunnerDeps

_logger = logging.getLogger(__name__)


def _same_message(frozen: ModelMessage, working: ModelMessage) -> bool:
    """Allow only the SDK's request preparation fields to differ in the send view."""
    if type(frozen) is not type(working):
        return False
    view = deepcopy(working)
    if isinstance(frozen, ModelRequest) and isinstance(view, ModelRequest):
        view.timestamp, view.instructions = frozen.timestamp, frozen.instructions
        # SDK fills missing identities on a tool-result request at its next send.
        if frozen.run_id is None:
            view.run_id = None
        if frozen.conversation_id is None:
            view.conversation_id = None
    return view == frozen


class Recorder:
    """Own one execution's acknowledged frozen prefix and sealed numeric boundary."""

    def __init__(self, messages: Sequence[ModelMessage] = (), *, resuming: bool = False):
        count = marked_prefix(messages, complete=False)
        self.frozen = deepcopy(list(messages[:count]))
        self.boundary = message_boundary(self.frozen)
        self.initial_saved = resuming
        self.stopped: BaseException | None = None

    def ensure_running(self) -> None:
        if self.stopped is not None:
            raise self.stopped

    def validate_messages(self, messages: Sequence[ModelMessage]) -> None:
        """Reject business edits/reordering/removal and newly occupied old positions."""
        self.ensure_running()
        if len(messages) < len(self.frozen) or any(
            not _same_message(saved, live)
            for saved, live in zip(self.frozen, messages[: len(self.frozen)], strict=True)
        ):
            raise ValueError("Published messages cannot be changed, removed or reordered")
        for message in messages[len(self.frozen) :]:
            if "seq" in (message.metadata or {}):
                raise ValueError("New messages cannot occupy an already assigned position")

    async def before_model_request(
        self, ctx: RunContext[RunnerDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        """Freeze the initial request after business preparation, before dispatch."""
        self.validate_messages(ctx.messages)
        if not self.initial_saved:
            messages = deepcopy(ctx.messages)
            request = messages[-1]
            if not isinstance(request, ModelRequest):
                raise ValueError("Initial checkpoint requires a complete model request")
            parts = request_context.model_request_parameters.instruction_parts
            if parts is not None:
                request.instructions = InstructionPart.join(parts)
            await self.record_messages(
                ctx.deps,
                ctx.messages,
                checkpoint="before_model_request",
                next=ModelRequestNext(),
                prepared=messages,
            )
            self.initial_saved = True
        ctx.deps.response_seq = self.boundary + 1 + len(ctx.messages) - len(self.frozen)
        return request_context

    async def record_messages(
        self,
        deps: RunnerDeps,
        messages: list[ModelMessage],
        *,
        checkpoint: CheckpointKind,
        next: Next,
        release: bool = False,
        prepared: list[ModelMessage] | None = None,
    ) -> None:
        """Freeze only the new suffix; acknowledge live seq before best-effort broadcast.

        A pending NodeResult.request is included by the caller without inserting it
        into SDK history. Even an empty suffix commits the new boundary and State.
        """
        try:
            self.validate_messages(messages)
            count = len(self.frozen)
            pending = deepcopy((prepared if prepared is not None else messages)[count:])
            batch = []
            for seq, message in enumerate(pending, start=self.boundary + 1):
                message.metadata = {**(message.metadata or {}), "seq": seq}
                batch.append(HistoryMessage(deps.session_id, seq, message))
            candidate = Checkpoint(
                checkpoint=checkpoint, messages=deepcopy(self.frozen) + pending, next=next
            )
            states = layer_registry.snapshot(deps)
            accepted = await self.persist_messages(
                deps, batch, candidate=candidate, layer_states=states, release=release
            )
        except BaseException as error:
            # SDK error-recovery hooks cannot turn an uncertain save into another
            # call or a new save in this execution.
            self.stopped = error
            raise
        # No await between acknowledged commit and local acknowledgment.
        for live, entry in zip(messages[count:], batch, strict=True):
            live.metadata = {**(live.metadata or {}), "seq": entry.seq}
        self.frozen = deepcopy(candidate.messages)
        self.boundary = message_boundary(self.frozen)
        try:
            if accepted:
                async with deps.output_service.publisher(deps.session_id) as publish:
                    for entry in accepted:
                        await publish(MessageCommitted(entry))
        except Exception:
            _logger.warning("Checkpoint committed; live broadcast unavailable", exc_info=True)

    async def persist_messages(
        self,
        deps: RunnerDeps,
        messages: list[HistoryMessage],
        *,
        candidate: Checkpoint,
        layer_states: dict,
        release: bool = False,
    ) -> tuple[HistoryMessage, ...]:
        """Atomically guard, append and save; never retry an uncertain commit reply."""
        async with deps.session_factory() as db, db.begin():
            repo, history = SessionRepository(db), HistoryRepository(db)
            row = await repo.guard(
                deps.session_id, deps.activity_id, deps.activity_run_id, (LifecycleStatus.READY,)
            )
            if await history.max_seq(deps.session_id) != self.boundary:
                raise ValueError("History boundary changed since checkpoint admission")
            if any(entry.seq <= self.boundary for entry in messages):
                raise ValueError("New messages cannot fill sealed history positions")
            accepted = await history.append_history(deps.session_id, messages)
            if len(accepted) != len(messages):
                raise ValueError("History did not accept the complete checkpoint batch")
            await repo._save_checkpoint(
                row,
                runner_state=candidate.model_dump_json(),
                layer_states=layer_states,
                release=release,
            )
        return accepted


class MessageRecordCapability(AbstractCapability["RunnerDeps"]):
    """Wrap successful nodes outside all business hooks, then checkpoint their result."""

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="outermost", wraps=[AbstractCapability])

    async def for_run(self, ctx: RunContext[RunnerDeps]) -> AbstractCapability[RunnerDeps]:
        from .resume import ResumeCapability

        return ResumeCapability() if ctx.deps.resume is not None else self

    async def wrap_run(self, ctx: RunContext[RunnerDeps], *, handler: WrapRunHandler):
        with capture_run_messages() as messages:
            ctx.deps.messages = messages
            return await handler()

    async def after_node_run(
        self,
        ctx: RunContext[RunnerDeps],
        *,
        node: AgentNode[RunnerDeps],
        result: NodeResult[RunnerDeps],
    ) -> NodeResult[RunnerDeps]:
        if isinstance(node, UserPromptNode):
            # UserPromptNode rebinds its list and normalizes message boundaries.
            # Restore complete stored records; the SDK normalizes its sending view.
            ctx.deps.messages[:] = deepcopy(ctx.messages)
        return result

    async def wrap_node_run(
        self,
        ctx: RunContext[RunnerDeps],
        *,
        node: AgentNode[RunnerDeps],
        handler: WrapNodeRunHandler[RunnerDeps],
    ) -> NodeResult[RunnerDeps]:
        recorder = ctx.deps.recorder
        if not isinstance(node, UserPromptNode):
            before = list(ctx.deps.messages)
            if isinstance(node, ModelRequestNode):
                before.append(node.request)
            recorder.validate_messages(before)
        result = await handler(node)
        if isinstance(node, (ModelRequestNode, CallToolsNode)):
            messages = list(ctx.deps.messages)
            if isinstance(result, ModelRequestNode):
                # SDK appends this request only when the next model node starts.
                messages.append(result.request)
                next = ModelRequestNext()
            elif isinstance(result, CallToolsNode):
                if not messages or result.model_response is not messages[-1]:
                    raise ValueError("Next tools node must handle the complete saved response")
                next = CallToolsNext()
            elif isinstance(result, End):
                next = FinishNext(
                    output=result.data.output,
                    tool_name=result.data.tool_name,
                    tool_call_id=result.data.tool_call_id,
                )
            else:
                raise ValueError("Unsupported custom checkpoint node")
            await recorder.record_messages(
                ctx.deps,
                messages,
                checkpoint=(
                    "model_node_completed"
                    if isinstance(node, ModelRequestNode)
                    else "tools_node_completed"
                ),
                next=next,
            )
        return result

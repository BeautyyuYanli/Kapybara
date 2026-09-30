"""Record the non-authoritative suffix at request, node and run boundaries.

Only authoritative SDK metadata anchors numbering. Non-authoritative messages
before the last anchor are silently ignored. Storage independently protects the
confirmed database prefix; discarded input is not restored into SDK history.
"""

from copy import deepcopy
from datetime import timedelta
from typing import Any

from pydantic_ai import ModelRequestNode, RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    AgentNode,
    CapabilityOrdering,
    NodeResult,
    WrapModelRequestHandler,
)
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.run import AgentRunResult
from temporalio import workflow

from kapy.agent_runner.types import HistoryMessage

from .types import MessageBatch, RunnerDeps


class MessageRecordCapability(AbstractCapability[RunnerDeps]):
    """Stateless recorder; acknowledged Activities alone mark live SDK messages."""

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="outermost", wraps=[AbstractCapability])

    async def after_node_run(
        self,
        ctx: RunContext[RunnerDeps],
        *,
        node: AgentNode[RunnerDeps],
        result: NodeResult[RunnerDeps],
    ) -> NodeResult[RunnerDeps]:
        if isinstance(node, ModelRequestNode):
            await self.record_messages(ctx.deps, ctx.messages, authoritative=True)
        return result

    async def after_run(
        self, ctx: RunContext[RunnerDeps], *, result: AgentRunResult[Any]
    ) -> AgentRunResult[Any]:
        await self.record_messages(ctx.deps, result.all_messages(), authoritative=False)
        return result

    async def record_messages(
        self, deps: RunnerDeps, messages: list[ModelMessage], *, authoritative: bool
    ) -> int:
        """Commit/broadcast copies, then mark live messages; return the next position.

        Non-authoritative marks are replaceable estimates. All messages after the
        last authoritative one are re-numbered, even if they already have a seq.
        Earlier non-authoritative messages are ignored without changing their marks.
        Empty suffixes perform no Activity. Failure leaves live metadata intact.
        """
        last_seq, start = -1, 0
        for index, message in enumerate(messages):
            metadata = message.metadata or {}
            seq = metadata.get("seq")
            authority = metadata.get("authoritative", False)
            if "seq" in metadata and (type(seq) is not int or seq < 0):
                raise UserError("History metadata.seq must be a nonnegative integer")
            if type(authority) is not bool:
                raise UserError("History metadata.authoritative must be a boolean")
            if authority:
                if seq is None:
                    raise UserError("Authoritative history requires metadata.seq")
                if seq <= last_seq:
                    raise UserError("Authoritative history seq must be strictly increasing")
                last_seq, start = seq, index + 1

        pending = messages[start:]
        batch = []
        for offset, message in enumerate(deepcopy(pending), start=last_seq + 1):
            message.metadata = {
                **(message.metadata or {}),
                "seq": offset,
                "authoritative": authoritative,
            }
            batch.append(HistoryMessage(deps.session_id, offset, authoritative, message))
        if batch:
            await workflow.execute_activity(
                "kapy.record_history",
                MessageBatch(session_id=deps.session_id, messages=batch),
                start_to_close_timeout=timedelta(seconds=30),
            )
            # The graph cannot advance while awaiting the Activity. Replay repeats
            # these assignments using its recorded completion, without external I/O.
            for message, entry in zip(pending, batch, strict=True):
                message.metadata = {
                    **(message.metadata or {}),
                    "seq": entry.seq,
                    "authoritative": entry.authoritative,
                }
        return last_seq + 1 + len(pending)


async def record_before_model_request(
    ctx: RunContext[RunnerDeps],
    *,
    request_context: ModelRequestContext,
    handler: WrapModelRequestHandler,
) -> ModelResponse:
    """Run after business request preparation, immediately outside Temporal dispatch."""
    recorder = next(
        capability
        for capability in ctx.capabilities.values()
        if isinstance(capability, MessageRecordCapability)
    )
    response_seq = await recorder.record_messages(ctx.deps, ctx.messages, authoritative=False)
    ctx.deps.response_seq = response_seq
    try:
        return await handler(request_context)
    finally:
        ctx.deps.response_seq = None

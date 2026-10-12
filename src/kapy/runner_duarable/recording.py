"""Predict response positions before dispatch and append immutable post-node history.

SDK metadata.seq marks successfully recorded messages. Storage independently
seals all positions through its last row; discarded input is not restored into
SDK history. Unrecorded run tails remain only in runner_state until another node.
"""

from copy import deepcopy
from datetime import timedelta

from pydantic_ai import ModelRequestNode, RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    AgentNode,
    CapabilityOrdering,
    NodeResult,
)
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestContext
from temporalio import workflow

from kapy.agent_history import HistoryMessage

from .types import MessageBatch, RunnerDeps


class Recorder:
    """Shared, stateless numbering and persistence for recording and prediction hooks.

    Run-local predictions live in RunnerDeps. Only acknowledged Activities mark
    live SDK messages; prediction never changes messages or performs an Activity.
    """

    def _pending_messages(self, messages: list[ModelMessage]) -> tuple[int, list[ModelMessage]]:
        """Validate stored positions and select the suffix after the last one."""
        last_seq, start = -1, 0
        for index, message in enumerate(messages):
            metadata = message.metadata or {}
            seq = metadata.get("seq")
            if "seq" in metadata and (type(seq) is not int or seq < 0):
                raise UserError("History metadata.seq must be a nonnegative integer")
            if seq is not None:
                if seq <= last_seq:
                    raise UserError("History seq must be strictly increasing")
                last_seq, start = seq, index + 1

        return last_seq + 1, messages[start:]

    async def predict_response_seq(
        self, ctx: RunContext[RunnerDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        """Predict from working history without recording, repairing or marking it.

        Post-node recording uses this same history, rather than the separately
        prepared model-facing list. Later requests overwrite the run-local prediction.
        """
        start_seq, pending = self._pending_messages(ctx.messages)
        ctx.deps.response_seq = start_seq + len(pending)
        return request_context

    async def record_messages(self, deps: RunnerDeps, messages: list[ModelMessage]) -> None:
        """Commit/broadcast copies, then mark live messages.

        Empty suffixes perform no Activity. Failure leaves live metadata intact.
        """
        start_seq, pending = self._pending_messages(messages)
        batch = []
        for offset, message in enumerate(deepcopy(pending), start=start_seq):
            message.metadata = {
                **(message.metadata or {}),
                "seq": offset,
            }
            batch.append(HistoryMessage(deps.session_id, offset, message))
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
                }


class MessageRecordCapability(AbstractCapability[RunnerDeps]):
    """Record model-node request/response history using an injected Recorder."""

    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

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
            await self.recorder.record_messages(ctx.deps, ctx.messages)
        return result

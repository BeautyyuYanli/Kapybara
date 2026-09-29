"""Record SDK history at node boundaries through a transactional Temporal Activity.

Only the unnumbered suffix is new. SDK merging can remove marks and cause that
suffix to overwrite earlier database rows. Retained marks must stay increasing;
edits before the last mark are ignored except for the final Request at run end.
"""

from copy import deepcopy
from datetime import timedelta
from typing import Any

from pydantic_ai import ModelRequestNode, RunContext
from pydantic_ai.capabilities import AbstractCapability, AgentNode, CapabilityOrdering, NodeResult
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessage, ModelRequest
from pydantic_ai.run import AgentRunResult
from temporalio import workflow

from .types import RecordHistoryInput, RunnerDeps


def _seq(message: ModelMessage) -> int | None:
    if message.metadata is None or "seq" not in message.metadata:
        return None
    seq = message.metadata["seq"]
    if type(seq) is not int or seq < 0:
        raise UserError("History metadata.seq must be a nonnegative integer")
    return seq


def _mark(message: ModelMessage, seq: int) -> None:
    message.metadata = {**(message.metadata or {}), "seq": seq}


class HistoryRecordCapability(AbstractCapability[RunnerDeps]):
    """Per-run flags track the initial empty history and first successful append.

    Sequence progress lives exclusively in the messages. Activity failures leave
    live messages and the explicit starting point untouched. Runs sharing an
    Agent receive fresh flags through for_run(), including during replay.
    """

    def __init__(self) -> None:
        self._initial_history_empty = False
        self._recorded = False

    async def for_run(self, ctx: RunContext[RunnerDeps]) -> HistoryRecordCapability:
        capability = HistoryRecordCapability()
        capability._initial_history_empty = not ctx.messages
        return capability

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
            await self._record(ctx.deps, ctx.messages)
        return result

    async def after_run(
        self, ctx: RunContext[RunnerDeps], *, result: AgentRunResult[Any]
    ) -> AgentRunResult[Any]:
        await self._record(ctx.deps, result.all_messages(), final=True)
        return result

    async def _record(
        self, deps: RunnerDeps, messages: list[ModelMessage], *, final: bool = False
    ) -> None:
        last_seq, start = -1, 0
        for index, message in enumerate(messages):
            seq = _seq(message)
            if seq is not None:
                if seq <= last_seq:
                    raise UserError("History seq marks must be strictly increasing")
                last_seq, start = seq, index + 1

        pending = messages[start:]
        numbered = deepcopy(pending)
        if pending:
            if not self._recorded and deps.next_seq is not None:
                next_seq = deps.next_seq
                if next_seq <= last_seq:
                    raise UserError("next_seq must be greater than the last history seq")
            elif last_seq >= 0:
                next_seq = last_seq + 1
            elif self._initial_history_empty and not self._recorded:
                next_seq = 0
            else:
                raise UserError(
                    "History has no seq anchor; an unused explicit next_seq is required"
                )
            for offset, message in enumerate(numbered):
                _mark(message, next_seq + offset)

        batch = list(numbered)
        if final:
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                if isinstance(message, ModelRequest):
                    if index < start:
                        if _seq(message) is None:
                            raise UserError("The final recorded Request has lost its seq")
                        batch.insert(0, deepcopy(message))
                    break
        if not batch:
            return

        await workflow.execute_activity(
            "kapy.record_history",
            RecordHistoryInput(session_id=deps.session_id, messages=batch),
            start_to_close_timeout=timedelta(seconds=30),
        )
        # Only acknowledged commits mark the live graph; its next node has not
        # run while awaiting this Activity. Replay repeats the same assignments.
        for message, saved in zip(pending, numbered, strict=True):
            seq = _seq(saved)
            assert seq is not None
            _mark(message, seq)
        if pending:
            self._recorded = True

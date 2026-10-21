"""Record immutable model-node history and save interrupted node-boundary snapshots.

SDK metadata.seq marks successfully recorded messages. Storage independently
seals all positions through its last row; discarded input is not restored into
SDK history. Unrecorded run tails remain only in runner_state until another model node.
Snapshots copy messages and layer State without releasing the current writer.
"""

from copy import deepcopy

from pydantic_ai import CallToolsNode, ModelRequestNode, RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    AgentNode,
    CapabilityOrdering,
    NodeResult,
)
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    RetryPromptPart,
)
from pydantic_ai.models import ModelRequestContext

from kapy.agent_history import HistoryMessage, HistoryRepository
from kapy.agent_output import MessageCommitted
from kapy.control.sessions.repository import SessionRepository
from kapy.layers.registry import layer_registry
from kapy.lifecycle import LifecycleStatus

from .types import RunnerDeps


class Recorder:
    """Stateless history numbering, recording and runner snapshot persistence.

    Run-local predictions live in RunnerDeps. Only acknowledged writes mark
    live SDK messages; prediction never changes messages or performs I/O.
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

        Empty suffixes perform no I/O. Failure leaves live metadata intact.
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
            await self.persist_messages(deps, batch)
            # Persist and publish finish before the graph can advance; failed
            # writes leave live SDK metadata unchanged.
            for message, entry in zip(pending, batch, strict=True):
                message.metadata = {
                    **(message.metadata or {}),
                    "seq": entry.seq,
                }

    async def persist_messages(self, deps: RunnerDeps, messages: list[HistoryMessage]) -> None:
        """Confirm the current owner, commit accepted messages, then publish them.

        Sealed positions are discarded. A missed broadcast is recovered by the
        existing history follower; no transaction spans publisher I/O.
        """
        async with deps.session_factory() as db, db.begin():
            session = await SessionRepository(db).guard(
                deps.session_id, deps.activity_id, deps.activity_run_id, (LifecycleStatus.READY,)
            )
            if session.id != deps.session_id:
                raise ValueError("Recording identity does not match session")
            accepted = await HistoryRepository(db).append_history(session.id, messages)
        if accepted:
            async with deps.output_service.publisher(deps.session_id) as publish:
                for message in accepted:
                    await publish(MessageCommitted(message))

    async def save_snapshot(self, deps: RunnerDeps, messages: list[ModelMessage]) -> None:
        """Save messages and validated State atomically, retaining write ownership.

        Only the detached trailing request is marked interrupted; live SDK messages and
        immutable history keep their original state. A later failure retains
        the last saved transition, rather than partial in-flight work.
        An interrupted request after pending calls makes the SDK synthesize
        interrupted tool returns on resume instead of executing those calls.
        """
        snapshot = deepcopy(messages)
        snapshot[-1].state = "interrupted"
        runner_state = ModelMessagesTypeAdapter.dump_json(snapshot).decode("utf-8")
        layer_states = layer_registry.snapshot(deps)
        async with deps.session_factory() as db, db.begin():
            session = await SessionRepository(db).guard(
                deps.session_id, deps.activity_id, deps.activity_run_id, (LifecycleStatus.READY,)
            )
            if layer_states.keys() != session.layers.keys():
                raise ValueError("Saved layer states must match configured layers")
            session.runner_state = runner_state
            session.layer_states = layer_states


class RunnerPersistenceCapability(AbstractCapability[RunnerDeps]):
    """Record model history; snapshot model→tool/model and tool→model transitions.

    Inner node hooks finish before persistence. A model→tool snapshot adds
    interruption feedback even for a plain text response; resumption always
    requests another model response. tool→end retains the preceding snapshot
    until the Activity saves final messages and State after resource cleanup.
    """

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
        if isinstance(node, (ModelRequestNode, CallToolsNode)):
            if isinstance(node, ModelRequestNode) and isinstance(result, CallToolsNode):
                messages = [
                    *ctx.messages,
                    ModelRequest(
                        parts=[
                            RetryPromptPart(
                                content=(
                                    "Unexpected interruption. "
                                    "Your messages may or may not be delivered. "
                                    "If your messages were intended to make changes, "
                                    "retry only if the operation is idempotent; otherwise, "
                                    "verify the outcome before taking further action."
                                )
                            )
                        ]
                    ),
                ]
            elif isinstance(result, ModelRequestNode):
                # The SDK appends this request only when its model node starts.
                messages = [*ctx.messages, result.request]
            else:
                return result
            await self.recorder.save_snapshot(ctx.deps, messages)
        return result

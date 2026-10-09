"""Confirm model inputs before dispatch and save provisional responses afterwards.

Only authoritative SDK metadata anchors numbering. Non-authoritative messages
before the last anchor are silently ignored. Storage independently protects the
confirmed database prefix; discarded input is not restored into SDK history.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
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
from pydantic_ai.models import Model, ModelRequestContext, ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.run import AgentRunResult
from pydantic_ai.settings import ModelSettings
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
            await self.record_messages(ctx.deps, ctx.messages, authoritative=False)
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


class _RecordingModel(WrapperModel):
    """One request's adapter; confirm the SDK's final input before dispatch.

    SDK 2.54 runs before hooks and message normalization inside the wrap handler.
    The model boundary receives their final result in both streaming modes.
    This adapter borrows the model and run context; neither is retained across runs.
    """

    def __init__(
        self, wrapped: Model, ctx: RunContext[RunnerDeps], recorder: MessageRecordCapability
    ) -> None:
        super().__init__(wrapped)
        self.ctx = ctx
        self.recorder = recorder

    @asynccontextmanager
    async def confirmed_input(self, messages: list[ModelMessage]) -> AsyncIterator[None]:
        response_seq = await self.recorder.record_messages(
            self.ctx.deps, messages, authoritative=True
        )
        # Retain prepared boundaries so later SDK normalization cannot discard
        # numbering metadata by merging adjacent confirmed requests.
        self.ctx.messages[:] = messages
        self.ctx.deps.response_seq = response_seq
        try:
            yield
        finally:
            self.ctx.deps.response_seq = None

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        async with self.confirmed_input(messages):
            return await self.wrapped.request(messages, model_settings, model_request_parameters)

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        async with self.confirmed_input(messages):
            async with self.wrapped.request_stream(
                messages, model_settings, model_request_parameters, run_context
            ) as response:
                yield response


async def record_before_model_request(
    ctx: RunContext[RunnerDeps],
    *,
    request_context: ModelRequestContext,
    handler: WrapModelRequestHandler,
) -> ModelResponse:
    """Install confirmation at the prepared model boundary before Temporal dispatch.

    Authority records the input's commitment, not provider receipt or cache use.
    The new response remains provisional until a later request includes it.
    """
    recorder = next(
        capability
        for capability in ctx.capabilities.values()
        if isinstance(capability, MessageRecordCapability)
    )
    return await handler(
        replace(request_context, model=_RecordingModel(request_context.model, ctx, recorder))
    )

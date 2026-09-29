"""Consume SDK Activity streams and broadcast provisional text through Worker output."""

from collections.abc import AsyncIterable
from typing import cast

from pydantic_ai import RunContext
from pydantic_ai.messages import AgentStreamEvent

from kapy.agent_output.deltas import to_text_delta

from .context import RunnerActivityContext
from .types import RunnerDeps


async def handle_deltas(
    ctx: RunContext[RunnerDeps], events: AsyncIterable[AgentStreamEvent]
) -> None:
    """Drain even non-model streams; each invocation owns and closes its publisher."""
    activity_ctx = cast(RunnerActivityContext, ctx)
    session_id, response_seq = ctx.deps.session_id, ctx.deps.response_seq
    async with activity_ctx.output_service.publisher(session_id) as publish:
        async for event in events:
            if response_seq is None:
                continue
            delta = to_text_delta(event, session_id=session_id, response_seq=response_seq)
            if delta is not None:
                await publish(delta)

"""Consume ordinary Agent streams and broadcast provisional text through Worker output."""

import logging
from collections.abc import AsyncIterable
from contextlib import AsyncExitStack

from pydantic_ai import RunContext
from pydantic_ai.messages import AgentStreamEvent

from kapy.agent_output.deltas import to_text_delta

from .types import RunnerDeps

_logger = logging.getLogger(__name__)


async def handle_deltas(
    ctx: RunContext[RunnerDeps], events: AsyncIterable[AgentStreamEvent]
) -> None:
    """Drain even non-model streams; each invocation owns and closes its publisher."""
    session_id, response_seq = ctx.deps.session_id, ctx.deps.response_seq
    stack, publish = AsyncExitStack(), None
    try:
        try:
            publish = await stack.enter_async_context(ctx.deps.output_service.publisher(session_id))
        except Exception:
            _logger.warning("Live previews unavailable", exc_info=True)
        async for event in events:
            if response_seq is None or publish is None:
                continue
            delta = to_text_delta(event, session_id=session_id, response_seq=response_seq)
            if delta is not None:
                try:
                    await publish(delta)
                except Exception:
                    _logger.warning("Live previews unavailable", exc_info=True)
                    publish = None
    finally:
        try:
            await stack.aclose()
        except Exception:
            _logger.warning("Unable to flush live previews", exc_info=True)

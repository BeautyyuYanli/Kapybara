"""Observe business model text without changing native node or hook execution order."""

from collections.abc import AsyncIterable, AsyncIterator
from typing import Any
from uuid import UUID

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import AgentStreamEvent

from kapy.agent_output.deltas import to_text_delta

from .types import OutputCallback


class OutputCapability(AbstractCapability[Any]):
    """Runner-owned observer; callback is run-scoped, response_seq is node-scoped.

    The SDK checks has_wrap_run_event_stream dynamically on each next(). Keeping
    this capability on the graph therefore supports enabling and disabling output
    between run calls without closing the graph or forcing disabled requests to stream.
    """

    def __init__(self, session_id: UUID) -> None:
        self.session_id = session_id
        self.callback: OutputCallback | None = None
        self.response_seq: int | None = None

    @property
    def has_wrap_run_event_stream(self) -> bool:
        return self.callback is not None and self.response_seq is not None

    async def wrap_run_event_stream(
        self, ctx: RunContext[Any], *, stream: AsyncIterable[AgentStreamEvent]
    ) -> AsyncIterator[AgentStreamEvent]:
        async for event in stream:
            callback, seq = self.callback, self.response_seq
            if callback is not None and seq is not None:
                delta = to_text_delta(event, session_id=self.session_id, response_seq=seq)
                if delta is not None:
                    await callback(delta)
            yield event

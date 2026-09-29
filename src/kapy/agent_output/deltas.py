"""Pure SDK text/thinking conversion shared by ordinary and Temporal runners."""

from uuid import UUID

from pydantic_ai.messages import (
    AgentStreamEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)

from kapy.agent_runner.types import TextDelta


def to_text_delta(
    event: AgentStreamEvent, *, session_id: UUID, response_seq: int
) -> TextDelta | None:
    """Keep whitespace and empty replacements; tools are expressed by full messages."""
    match event:
        case PartStartEvent(index=index, part=TextPart(content=text)):
            return TextDelta(session_id, response_seq, index, "text", "replace", text)
        case PartStartEvent(index=index, part=ThinkingPart(content=text)):
            return TextDelta(session_id, response_seq, index, "thinking", "replace", text)
        case PartDeltaEvent(index=index, delta=TextPartDelta(content_delta=text)) if text:
            return TextDelta(session_id, response_seq, index, "text", "append", text)
        case PartDeltaEvent(index=index, delta=ThinkingPartDelta(content_delta=text)) if text:
            return TextDelta(session_id, response_seq, index, "thinking", "append", text)
    return None

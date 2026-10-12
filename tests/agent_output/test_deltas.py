"""SDK stream previews preserve resets/whitespace without rendering tool arguments."""

from uuid import uuid4

from pydantic_ai.messages import (
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
)

from kapy.agent_output import TextDelta
from kapy.agent_output.deltas import to_text_delta


def test_part_resets_whitespace_and_nontext_events():
    session_id = uuid4()
    events = [
        PartStartEvent(index=0, part=TextPart("old")),
        PartStartEvent(index=0, part=TextPart("")),
        PartDeltaEvent(index=0, delta=TextPartDelta(" \n")),
        PartDeltaEvent(index=0, delta=TextPartDelta("")),
        PartStartEvent(index=1, part=ThinkingPart("")),
        PartDeltaEvent(index=1, delta=ThinkingPartDelta(signature_delta="opaque")),
        PartStartEvent(index=2, part=ToolCallPart("work", {}, "call")),
    ]
    assert [
        delta
        for event in events
        if (delta := to_text_delta(event, session_id=session_id, response_seq=9)) is not None
    ] == [
        TextDelta(session_id, 9, 0, "text", "replace", "old"),
        TextDelta(session_id, 9, 0, "text", "replace", ""),
        TextDelta(session_id, 9, 0, "text", "append", " \n"),
        TextDelta(session_id, 9, 1, "thinking", "replace", ""),
    ]

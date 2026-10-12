"""Ordinary Agent streams borrow Worker resources from per-run deps."""

import pytest
from pydantic_ai import RunContext
from pydantic_ai.messages import (
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ToolCallPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from kapy.agent_output import TextDelta
from kapy.runner.output import handle_deltas


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("response_seq", [7, None])
async def test_handler_drains_stream_and_flushes_text_with_optional_prediction(
    runner_database, response_seq
):
    from pydantic import TypeAdapter

    from kapy.agent_output import OutputEvent

    service, session_id = runner_database.outputs, runner_database.session_id
    deps = runner_database.deps()
    deps.response_seq = response_seq
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    consumed = []
    source = [
        PartStartEvent(index=0, part=TextPart("")),
        PartDeltaEvent(index=0, delta=TextPartDelta(" ")),
        PartDeltaEvent(index=0, delta=TextPartDelta("x")),
        PartStartEvent(index=1, part=ThinkingPart("reasoning")),
        PartStartEvent(index=2, part=ToolCallPart("tool", {"arg": "value"})),
    ]

    async def events():
        for event in source:
            consumed.append(event)
            yield event

    async with service._client.pubsub() as raw:
        await raw.subscribe(f"{service._channel_prefix}:{session_id}")
        await raw.get_message(timeout=1)
        await handle_deltas(ctx, events())
        packet = await raw.get_message(timeout=0.1)
        if response_seq is None:
            assert packet is None
        else:
            assert TypeAdapter(list[OutputEvent]).validate_json(packet["data"]) == [
                TextDelta(session_id, 7, 0, "text", "replace", " x"),
                TextDelta(session_id, 7, 1, "thinking", "replace", "reasoning"),
            ]
    assert consumed == source

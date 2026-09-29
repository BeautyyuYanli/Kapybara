"""SDK context reconstruction and Activity-stream consumption use Worker resources."""

from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic_ai import RunContext
from pydantic_ai.durable_exec.temporal import PydanticAIPayloadConverter
from pydantic_ai.exceptions import UserError
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
from temporalio.converter import DataConverter
from valkey.asyncio import Valkey

from kapy.agent_output import AgentOutputService
from kapy.agent_runner import TextDelta
from kapy.runner_duarable import DurableExecutionConfig, RunnerDeps
from kapy.runner_duarable.context import RunnerActivityContext, bind_output_service
from kapy.runner_duarable.output import handle_deltas


def deps(session_id=None, response_seq=None):
    return RunnerDeps(
        config=DurableExecutionConfig(
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="pydantic_ai.models.openai:OpenAIChatModel",
            model_name="test",
            api_key="test-secret",
        ),
        session_id=session_id or uuid4(),
        response_seq=response_seq,
    )


@pytest.mark.asyncio
async def test_default_sdk_context_codec_and_copy_inject_bound_service():
    ctx = RunContext(deps=deps(response_seq=12), model=TestModel(), usage=RunUsage())
    converter = DataConverter(payload_converter_class=PydanticAIPayloadConverter)
    encoded = await converter.encode([RunnerActivityContext.serialize_run_context(ctx), ctx.deps])
    serialized, restored_deps = await converter.decode(encoded, [dict, RunnerDeps])
    async with Valkey() as client:
        service = AgentOutputService(client)
        with bind_output_service(service):
            restored = RunnerActivityContext.deserialize_run_context(serialized, restored_deps)
            copied = replace(restored, run_step=2)
            assert copied.output_service is restored.output_service is service
            assert copied.deps == ctx.deps and copied.run_step == 2
            # Re-encoding stays within the SDK's serializable fields even after injection.
            assert await converter.encode([RunnerActivityContext.serialize_run_context(restored)])
        with pytest.raises(UserError, match="not bound"):
            RunnerActivityContext.deserialize_run_context(serialized, restored_deps)


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize("response_seq", [7, None])
async def test_handler_drains_stream_and_flushes_text_with_optional_prediction(
    runner_database, response_seq
):
    from pydantic import TypeAdapter

    from kapy.agent_runner import OutputEvent

    service, session_id = runner_database.outputs, runner_database.session_id
    ctx = RunnerActivityContext(deps(session_id, response_seq))
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

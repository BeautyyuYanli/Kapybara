"""Executable contracts for SDK internals used at durable checkpoint boundaries.

These tests intentionally use real nodes and message adapters. They should fail
when an SDK upgrade changes message mutation timing or recovery entry semantics.
"""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic_ai import Agent, CallToolsNode, ModelRequestNode, UserPromptNode
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_graph import End

from kapy.agent_runner.auxiliary import run_auxiliary

pytestmark = pytest.mark.asyncio


async def test_input_preparation_preserves_system_and_user_parts():
    agent = Agent(TestModel(), system_prompt="static")

    @agent.system_prompt(dynamic=True)
    def dynamic() -> str:
        return "dynamic"

    async with agent.iter("first") as native:
        initial = native.next_node
        assert isinstance(initial, UserPromptNode)
        request = await native.next(initial)
        assert isinstance(request, ModelRequestNode)
        assert [p.content for p in request.request.parts if isinstance(p, SystemPromptPart)] == [
            "static",
            "dynamic",
        ]
        saved = deepcopy(request.request)
        assert native.all_messages() == []
    history = [saved, ModelRequest(parts=[UserPromptPart("steer")])]
    async with agent.iter(None, message_history=deepcopy(history)) as native:
        initial = native.next_node
        assert isinstance(initial, UserPromptNode)
        request = await native.next(initial)
        assert isinstance(request, ModelRequestNode)
        response = await native.next(request)
        assert isinstance(response, CallToolsNode)
        assert [
            p.content
            for m in native.all_messages()
            if isinstance(m, ModelRequest)
            for p in m.parts
            if isinstance(p, UserPromptPart)
        ] == ["first", "steer"]


async def test_tool_request_exists_before_it_enters_history_and_text_end_has_no_delta():
    agent = Agent(TestModel())

    @agent.tool_plain
    def work() -> str:
        return "ok"

    async with agent.iter("go") as native:
        initial = native.next_node
        assert isinstance(initial, UserPromptNode)
        request = await native.next(initial)
        assert isinstance(request, ModelRequestNode)
        response = await native.next(request)
        assert isinstance(response, CallToolsNode)
        assert isinstance(response.model_response.parts[0], ToolCallPart)
        before_tools = len(native.all_messages())
        next_request = await native.next(response)
        assert isinstance(next_request, ModelRequestNode)
        assert isinstance(next_request.request.parts[0], ToolReturnPart)
        assert len(native.all_messages()) == before_tools
        following = await native.next(next_request)
        assert isinstance(following, CallToolsNode)
        assert len(native.all_messages()) == before_tools + 2
        before_end = len(native.all_messages())
        end = await native.next(following)
        assert isinstance(end, End)
        assert native.all_messages()[before_end:] == []
        assert native.result is not None
        assert len(native.result.new_messages()) == 4


async def test_output_tool_end_appends_only_closing_request():
    agent = Agent(TestModel(custom_output_args=["one"]), output_type=list[str])
    async with agent.iter("go") as native:
        initial = native.next_node
        assert isinstance(initial, UserPromptNode)
        request = await native.next(initial)
        assert isinstance(request, ModelRequestNode)
        response = await native.next(request)
        assert isinstance(response, CallToolsNode)
        before_end = len(native.all_messages())
        end = await native.next(response)
        assert isinstance(end, End)
        delta = native.all_messages()[before_end:]
        assert len(delta) == 1
        assert isinstance(delta[0], ModelRequest)
        assert isinstance(delta[0].parts[0], ToolReturnPart)
        assert end.data.output == ["one"]


async def test_official_adapter_and_explicit_response_recovery_preserve_call_ids():
    model_calls = []

    def unexpected(messages, info):
        model_calls.append(1)
        return ModelResponse(parts=[TextPart("unused")])

    agent = Agent(FunctionModel(unexpected), instructions="instructions")

    @agent.tool_plain
    def work() -> str:
        return "ok"

    history = [
        ModelRequest(parts=[UserPromptPart("go")], metadata={"source": "test"}),
        ModelResponse(
            parts=[ToolCallPart("work", {}, "call-id")],
            finish_reason="tool_call",
            provider_response_id="response-id",
        ),
    ]
    payload = ModelMessagesTypeAdapter.dump_json(history)
    restored = ModelMessagesTypeAdapter.validate_json(payload)
    assert restored == history
    async with agent.iter(None, message_history=restored) as native:
        initial = native.next_node
        assert isinstance(initial, UserPromptNode)
        await native.next(initial)
        response = native.all_messages()[-1]
        assert isinstance(response, ModelResponse)
        following = await native.next(CallToolsNode(response))
        assert isinstance(following, ModelRequestNode)
        part = following.request.parts[0]
        assert isinstance(part, ToolReturnPart)
        assert part.tool_call_id == "call-id"
    assert model_calls == []


@pytest.mark.parametrize("structured,block_tools", [(False, True), (True, True), (True, False)])
async def test_auxiliary_retries_preserve_definitions_and_isolate_history(structured, block_tools):
    from pydantic import BaseModel

    class Result(BaseModel):
        count: int

    received, executed, validated = [], [], []

    def model(messages, info):
        received.append((deepcopy(messages), deepcopy(info)))
        if len(received) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart("work", {}, "a"),
                    ToolCallPart("work", {}, "b"),
                ]
            )
        if structured and len(received) == 2:
            return ModelResponse(parts=[TextPart('{"count":"3"}')])
        return ModelResponse(parts=[TextPart('{"count":3}' if structured else "summary")])

    agent = Agent(FunctionModel(model), instructions="stable", model_settings={"temperature": 0.25})

    @agent.system_prompt(dynamic=True)
    def dynamic() -> str:
        return "reevaluated system"

    @agent.tool_plain
    def work() -> str:
        executed.append(True)
        return "ok"

    @agent.output_validator
    def validate(output: str) -> str:
        validated.append(True)
        return output + " validated"

    # Actual SDK prompt reevaluation replaces an existing ModelRequest's parts.
    # Sharing those nested message objects would mutate the caller's input.
    history = [
        ModelRequest(
            parts=[
                SystemPromptPart("original system", dynamic_ref=dynamic.__qualname__),
                UserPromptPart("original"),
            ]
        ),
        ModelResponse(parts=[TextPart("previous answer")]),
    ]
    before = deepcopy(history)
    session_id = uuid4()
    result = await run_auxiliary(
        agent,
        "summarize",
        history=history,
        session_id=session_id,
        deps=None,
        capabilities=[],
        result_type=Result if structured else None,
        block_other_tools=block_tools,
    )
    assert result == (Result(count=3) if structured else "summary validated")
    assert executed == ([] if block_tools else [True, True])
    assert validated == ([] if structured else [True])
    assert history == before
    assert [
        part.content for part in received[0][0][0].parts if isinstance(part, SystemPromptPart)
    ] == ["reevaluated system"]
    assert len(received) == (3 if structured else 2)
    if structured:
        # A coercible string is still invalid: the SDK must retry before accepting int.
        assert any(isinstance(part, RetryPromptPart) for part in received[2][0][-1].parts)
    for _, info in received:
        assert info.model_settings == {"temperature": 0.25}
        assert [tool.name for tool in info.function_tools] == ["work"]
        assert info.output_tools == [] and info.allow_text_output
    replies = received[1][0][-1].parts
    reply_type = RetryPromptPart if block_tools else ToolReturnPart
    assert [(p.tool_name, p.tool_call_id) for p in replies if isinstance(p, reply_type)] == [
        ("work", "a"),
        ("work", "b"),
    ]
    assert received[1][0][-2].conversation_id == str(session_id)


async def test_auxiliary_structured_retry_budget_is_bounded():
    from pydantic import BaseModel

    class Result(BaseModel):
        count: int

    calls = []

    def model(messages, info):
        calls.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("invalid JSON")])

    with pytest.raises(UnexpectedModelBehavior):
        await run_auxiliary(
            Agent(FunctionModel(model)),
            "task",
            history=[],
            session_id=uuid4(),
            deps=None,
            capabilities=[],
            result_type=Result,
        )
    assert len(calls) == 3
    assert len([m for m in calls[-1] if isinstance(m, ModelResponse)]) == 2


async def test_output_observer_preserves_part_resets_and_ignores_nontext_events():
    from pydantic_ai import RunContext
    from pydantic_ai.messages import (
        PartDeltaEvent,
        PartStartEvent,
        TextPartDelta,
        ThinkingPartDelta,
    )
    from pydantic_ai.usage import RunUsage

    from kapy.agent_runner import TextDelta
    from kapy.agent_runner.output import OutputCapability

    session_id = uuid4()
    observer = OutputCapability(session_id)
    observer.response_seq = 9
    received = []

    async def callback(event):
        received.append(event)

    observer.callback = callback
    original = [
        PartStartEvent(index=0, part=TextPart("old")),
        PartStartEvent(index=0, part=TextPart("")),
        PartDeltaEvent(index=0, delta=TextPartDelta(" \n")),
        PartDeltaEvent(index=0, delta=TextPartDelta("")),
        PartStartEvent(index=1, part=ThinkingPart("")),
        PartDeltaEvent(index=1, delta=ThinkingPartDelta(signature_delta="opaque")),
        PartStartEvent(index=2, part=ToolCallPart("work", {}, "call")),
    ]

    async def events():
        for event in original:
            yield event

    ctx = RunContext(deps=None, model=TestModel(), usage=RunUsage())
    assert [
        event async for event in observer.wrap_run_event_stream(ctx, stream=events())
    ] == original
    assert received == [
        TextDelta(session_id, 9, 0, "text", "replace", "old"),
        TextDelta(session_id, 9, 0, "text", "replace", ""),
        TextDelta(session_id, 9, 0, "text", "append", " \n"),
        TextDelta(session_id, 9, 1, "thinking", "replace", ""),
    ]


async def test_auxiliary_blocking_rejects_native_tools_before_model_request():
    from pydantic_ai.native_tools import WebSearchTool

    calls = []

    def model(messages, info):
        calls.append(True)
        return ModelResponse(parts=[TextPart("should not run")])

    from pydantic_ai.capabilities import NativeTool

    agent = Agent(FunctionModel(model), capabilities=[NativeTool(WebSearchTool())])
    with pytest.raises(ValueError, match="native server tools"):
        await run_auxiliary(
            agent,
            "task",
            history=[],
            session_id=uuid4(),
            deps=None,
            capabilities=[],
            block_other_tools=True,
        )
    assert calls == []

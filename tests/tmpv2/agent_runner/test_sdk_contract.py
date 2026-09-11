"""Executable contracts for SDK internals used at durable checkpoint boundaries.

These tests intentionally use real nodes and message adapters. They should fail
when an SDK upgrade changes message mutation timing or recovery entry semantics.
"""

from copy import deepcopy

import pytest
from pydantic_ai import Agent, CallToolsNode, ModelRequestNode, UserPromptNode
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_graph import End

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

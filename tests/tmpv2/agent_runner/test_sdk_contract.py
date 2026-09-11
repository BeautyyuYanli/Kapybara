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

from kapy.tmpv2.agent_runner.compaction import summarize

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


async def test_compaction_retries_raw_output_without_tools_validators_or_config_changes():
    received = []

    def model(messages, info):
        received.append(deepcopy(messages))
        assert info.model_settings == {"temperature": 0.25}
        assert [tool.name for tool in info.function_tools] == ["work"]
        assert [tool.name for tool in info.output_tools] == ["final_result"]
        assert not info.allow_text_output
        if len(received) == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart("work", {}, "work-id"),
                    ToolCallPart("final_result", {"response": ["not a summary"]}, "output-id"),
                ]
            )
        return ModelResponse(
            parts=[ThinkingPart("hidden"), TextPart(" summary"), TextPart(" text ")]
        )

    agent = Agent(FunctionModel(model), output_type=list[str], model_settings={"temperature": 0.25})

    @agent.tool_plain
    def work() -> str:
        raise AssertionError("compaction must not execute client tools")

    @agent.output_validator
    def validate(output: list[str]) -> list[str]:
        raise AssertionError("compaction must not execute business output validators")

    history = [ModelRequest(parts=[UserPromptPart("original")])]
    before = deepcopy(history)
    session_id = uuid4()
    assert (
        await summarize(agent, history, session_id=session_id, deps=None, max_retries=1)
        == "summary text"
    )
    assert history == before
    retry = received[1][-1]
    assert isinstance(retry, ModelRequest)
    assert [
        (part.tool_name, part.tool_call_id)
        for part in retry.parts
        if isinstance(part, RetryPromptPart)
    ] == [("work", "work-id"), ("final_result", "output-id")]
    assert isinstance(received[1][-2], ModelResponse)
    assert received[1][-2].conversation_id == str(session_id)


@pytest.mark.parametrize("incomplete", [False, True])
async def test_compaction_empty_or_incomplete_response_has_bounded_failure(incomplete):
    calls = []

    def model(messages, info):
        calls.append(deepcopy(messages))
        return ModelResponse(
            parts=[TextPart(" ")], state="incomplete" if incomplete else "complete"
        )

    with pytest.raises(UnexpectedModelBehavior):
        await summarize(
            Agent(FunctionModel(model)), [], session_id=uuid4(), deps=None, max_retries=2
        )
    assert len(calls) == (1 if incomplete else 3)
    if not incomplete:
        assert isinstance(calls[-1][-1].parts[0], RetryPromptPart)
        assert len([m for m in calls[-1] if isinstance(m, ModelResponse)]) == 2

"""Public Agent/run contracts with the real SDK and CodeMode sandbox."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from typing import Any

import httpx2
import pytest
from openai import AsyncOpenAI, DefaultAsyncHttpxClient
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    Capability,
    CapabilityOrdering,
    NativeTool,
    PrefixTools,
    WrapModelRequestHandler,
)
from pydantic_ai.messages import (
    CapabilityInstructionSource,
    InstructionId,
    InstructionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.native_tools import WebSearchTool
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai.usage import RequestUsage, UsageLimits

from kapy.agent_behavior import AgentBehaviorCapability

pytestmark = pytest.mark.asyncio


async def test_replaces_all_sources_and_preserves_configured_parts():
    configured = [
        InstructionPart(
            "dynamic configuration",
            dynamic=True,
            name="first",
            id=InstructionId(CapabilityInstructionSource("x"), name="first"),
        ),
        InstructionPart(
            "static configuration",
            name="second",
            id=InstructionId(CapabilityInstructionSource("x"), name="second"),
        ),
    ]
    expected = deepcopy(configured)
    policy = AgentBehaviorCapability(configured, tool_whitelist=[])
    configured[0].content = "caller mutation"
    configured.clear()
    requests = []

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parts = info.model_request_parameters.instruction_parts
        assert parts == expected
        assert parts is not None
        requests.append(parts)
        parts[0].content = "request mutation"
        return ModelResponse(parts=[TextPart("done")])

    def hidden() -> str:
        return "hidden"

    agent = Agent(
        FunctionModel(model),
        instructions="agent static",
        toolsets=[FunctionToolset([hidden], instructions="toolset static")],
        capabilities=[policy, Capability(instructions="capability static")],
    )

    @agent.instructions
    def dynamic() -> str:
        return "agent dynamic"

    for _ in range(2):
        result = await agent.run("go", instructions="run static")
        request = result.new_messages()[0]
        assert isinstance(request, ModelRequest)
        assert request.instructions == InstructionPart.join(expected)
    assert requests[0] is not requests[1]
    assert requests[0][0] is not requests[1][0]


@pytest.mark.parametrize("protocol", ["chat", "responses"])
@pytest.mark.parametrize("instructions", [None, [], [InstructionPart("configured")]])
async def test_wire_instructions_preserve_system_messages_and_history(protocol, instructions):
    requests = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if protocol == "chat":
            assert {tool["function"]["name"] for tool in payload["tools"]} == {"run_code"}
            system_messages = [m["content"] for m in payload["messages"] if m["role"] == "system"]
            assert system_messages == (
                ["system message", "configured"] if instructions else ["system message"]
            )
            return httpx2.Response(
                200,
                json={
                    "id": "chat-1",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "test",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {"role": "assistant", "content": "done"},
                        },
                    ],
                },
            )
        assert {tool["name"] for tool in payload["tools"]} == {"run_code"}
        assert payload.get("instructions") == ("configured" if instructions else None)
        system_messages = [m for m in payload["input"] if m.get("role") == "system"]
        assert system_messages == [{"role": "system", "content": "system message"}]
        return httpx2.Response(
            200,
            json={
                "id": "response-1",
                "object": "response",
                "created_at": 1,
                "model": "test",
                "status": "completed",
                "output": [
                    {
                        "id": "message-1",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "done", "annotations": []}],
                    },
                ],
            },
        )

    async with DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handle)) as http:
        async with AsyncOpenAI(
            api_key="test", base_url="https://model.example/v1", http_client=http
        ) as client:
            model_class = OpenAIChatModel if protocol == "chat" else OpenAIResponsesModel
            agent = Agent(
                model_class("test", provider=OpenAIProvider(openai_client=client)),
                instructions="new static instructions",
                system_prompt="system message",
                capabilities=[AgentBehaviorCapability(instructions, tool_whitelist=[])],
            )
            previous = await agent.run("first prompt")
            history = previous.all_messages()
            saved = deepcopy(history)
            result = await agent.run("new prompt", message_history=history)
    assert result.output == "done"
    assert len(requests) == 2
    assert history == saved
    assert result.all_messages()[: len(saved)] == saved
    assert isinstance(history[0], ModelRequest)
    assert [part.content for part in history[0].parts if isinstance(part, SystemPromptPart)] == [
        "system message"
    ]
    latest = next(m for m in reversed(result.all_messages()) if isinstance(m, ModelRequest))
    assert latest.instructions == ("configured" if instructions else None)


@pytest.mark.parametrize("registration", ["agent", "run"])
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("instructions", [None, [], [InstructionPart("configured")]])
async def test_hidden_tools_run_in_code_mode_with_an_empty_whitelist(
    registration, streaming, instructions
):
    calls = []
    request_count = 0

    def hidden(value: int) -> int:
        calls.append(value)
        return value * 2

    def response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal request_count
        request_count += 1
        assert [tool.name for tool in info.function_tools] == ["run_code"]
        assert info.instructions == InstructionPart.join(instructions or [])
        assert "hidden(" not in (info.function_tools[0].description or "")
        if request_count == 1:
            return ModelResponse(
                parts=[ToolCallPart("run_code", {"code": "memo = await hidden(value=7)\nmemo"})]
            )
        result = messages[-1]
        assert isinstance(result, ModelRequest)
        returned = result.parts[0]
        assert isinstance(returned, ToolReturnPart)
        if request_count == 2:
            assert returned.content == 14
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": "memo + 1"})])
        assert returned.content == 15
        return ModelResponse(parts=[TextPart("done")])

    async def stream(messages: list[ModelMessage], info: AgentInfo):
        part = response(messages, info).parts[0]
        if isinstance(part, ToolCallPart):
            yield {0: DeltaToolCall(name=part.tool_name, json_args=json.dumps(part.args))}
        else:
            assert isinstance(part, TextPart)
            yield part.content

    policy = AgentBehaviorCapability(instructions, tool_whitelist=[])
    agent = Agent(
        FunctionModel(response, stream_function=stream),
        tools=[hidden],
        capabilities=[policy] if registration == "agent" else [],
    )
    run_capabilities = [policy] if registration == "run" else []
    if streaming:
        async with agent.run_stream("go", capabilities=run_capabilities) as result:
            assert await result.get_output() == "done"
    else:
        result = await agent.run("go", capabilities=run_capabilities)
        assert result.output == "done"
    assert calls == [7]
    assert request_count == 3


@pytest.mark.parametrize("whitelist", [{"acme_first", "acme_second"}, {"first", "second"}])
async def test_whitelist_uses_final_names_and_keeps_direct_tools_in_order(whitelist):
    calls = []
    expected = (
        ["acme_first", "acme_second", "run_code"] if "acme_first" in whitelist else ["run_code"]
    )
    policy = AgentBehaviorCapability(None, tool_whitelist=whitelist)
    whitelist.clear()

    def first(value: int) -> int:
        calls.append(value)
        return value

    def second() -> str:
        return "second"

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert [tool.name for tool in info.function_tools] == expected
        if len(messages) == 1 and len(expected) > 1:
            first_def = info.function_tools[0]
            assert first_def.parameters_json_schema["properties"]["value"]["type"] == "integer"
            return ModelResponse(parts=[ToolCallPart("acme_first", {"value": 3})])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(model),
        capabilities=[policy, PrefixTools(Capability(tools=[first, second]), prefix="acme")],
    )
    result = await agent.run("go")
    assert result.output == "done"
    assert calls == ([3] if len(expected) > 1 else [])


@pytest.mark.parametrize("policy_first", [True, False])
@pytest.mark.parametrize("streaming", [True, False])
async def test_policy_runs_after_wrap_and_before_preparation(policy_first, streaming):
    counted = []

    class LatePreparation(AbstractCapability[Any]):
        def get_ordering(self) -> CapabilityOrdering:
            return CapabilityOrdering(position="innermost")

        async def wrap_model_request(
            self,
            ctx: RunContext[Any],
            *,
            request_context: ModelRequestContext,
            handler: WrapModelRequestHandler,
        ) -> ModelResponse:
            parameters = request_context.model_request_parameters
            return await handler(
                replace(
                    request_context,
                    model_request_parameters=replace(
                        parameters,
                        instruction_parts=[InstructionPart("late instructions")],
                        function_tools=[
                            *parameters.function_tools,
                            ToolDefinition(name="late_tool"),
                        ],
                    ),
                )
            )

        async def before_model_request(
            self, ctx: RunContext[Any], request_context: ModelRequestContext
        ) -> ModelRequestContext:
            parameters = request_context.model_request_parameters
            return replace(
                request_context,
                model_request_parameters=replace(
                    parameters,
                    instruction_parts=[InstructionPart("before instructions")],
                    function_tools=[*parameters.function_tools, ToolDefinition(name="before_tool")],
                ),
            )

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert info.instructions == "configured"
        assert [tool.name for tool in info.function_tools] == ["run_code"]
        return ModelResponse(parts=[TextPart("done")])

    async def stream(messages: list[ModelMessage], info: AgentInfo):
        part = model(messages, info).parts[0]
        assert isinstance(part, TextPart)
        yield part.content

    class CountingModel(FunctionModel):
        async def count_tokens(self, messages, model_settings, model_request_parameters):
            counted.append(deepcopy(model_request_parameters))
            return RequestUsage(input_tokens=1)

    capabilities = [
        AgentBehaviorCapability([InstructionPart("configured")], tool_whitelist=[]),
        LatePreparation(),
    ]
    if not policy_first:
        capabilities.reverse()
    agent = Agent(CountingModel(model, stream_function=stream), capabilities=capabilities)
    limits = UsageLimits(count_tokens_before_request=True)
    if streaming:
        async with agent.run_stream("go", usage_limits=limits) as result:
            assert await result.get_output() == "done"
    else:
        result = await agent.run("go", usage_limits=limits)
        assert result.output == "done"
    assert len(counted) == 1
    assert counted[0].instruction_parts == [InstructionPart("configured")]
    assert [tool.name for tool in counted[0].function_tools] == ["run_code"]


async def test_output_tools_native_tools_and_settings_survive_output_retry():
    class Result(BaseModel):
        count: int

    requests = []
    native = WebSearchTool()

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        parameters = info.model_request_parameters
        assert parameters.native_tools == [native]
        assert info.model_settings == {"temperature": 0.2, "max_tokens": 200}
        assert not info.allow_text_output
        assert len(info.output_tools) == 1
        assert [tool.name for tool in info.function_tools] == ["run_code"]
        assert parameters.instruction_parts == [InstructionPart("configured")]
        requests.append(parameters)
        args = {"count": "invalid"} if len(requests) == 1 else {"count": 3}
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, args)])

    agent = Agent(
        FunctionModel(model),
        output_type=Result,
        model_settings={"temperature": 0.2, "max_tokens": 200},
        capabilities=[
            AgentBehaviorCapability([InstructionPart("configured")], tool_whitelist=[]),
            NativeTool(native),
        ],
    )
    result = await agent.run("go")
    assert result.output == Result(count=3)
    assert len(requests) == 2


async def test_shared_capability_keeps_concurrent_code_sessions_separate():
    policy = AgentBehaviorCapability(None, tool_whitelist=[])
    started = asyncio.Event()
    continue_first = asyncio.Event()
    first_calls = 0
    second_calls = 0

    async def first_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal first_calls
        first_calls += 1
        if first_calls == 1:
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": "memo = 42\nmemo"})])
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        returned = request.parts[0]
        assert isinstance(returned, ToolReturnPart)
        assert returned.content == 42
        if first_calls == 2:
            started.set()
            await continue_first.wait()
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": "memo"})])
        return ModelResponse(parts=[TextPart("first done")])

    async def second_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal second_calls
        second_calls += 1
        if second_calls == 1:
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": "memo"})])
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        assert isinstance(request.parts[0], RetryPromptPart)
        assert "memo" in request.parts[0].content
        return ModelResponse(parts=[TextPart("second done")])

    first = Agent(FunctionModel(first_model), capabilities=[policy])
    second = Agent(FunctionModel(second_model), capabilities=[policy])
    async with asyncio.TaskGroup() as tasks:
        first_run = tasks.create_task(first.run("first"))
        await asyncio.wait_for(started.wait(), timeout=10)
        second_run = tasks.create_task(second.run("second"))
        await second_run
        continue_first.set()
    assert first_run.result().output == "first done"
    assert second_run.result().output == "second done"
    assert first_calls == 3
    assert second_calls == 2

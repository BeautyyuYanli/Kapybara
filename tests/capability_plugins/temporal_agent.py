"""Temporal test fixture; resource operations assert that they stay in Activities."""

import json
from dataclasses import replace
from datetime import timedelta
from typing import Any, Self

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import AbstractCapability, Capability, CombinedCapability
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests, ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from capability_plugins.tool_definitions import build_approval
from kapy.capability_plugins import PluginDeps, PluginRegistry
from kapy.capability_plugins.examples.echo import EchoConfig, build_echo

events: list[tuple[str, str]] = []


class ResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str


class ResourceToolset(FunctionToolset[PluginDeps]):
    def __init__(self, label: str) -> None:
        self.resource_label = label
        self.entered = False

        async def resource_echo(text: str) -> str:
            assert activity.in_activity() and self.entered
            events.append(("call", label))
            return f"resource:{label}:{text}"

        async def validate(ctx: RunContext[PluginDeps], text: str) -> None:
            assert activity.in_activity() and self.entered
            assert text == "hello"
            events.append(("validate", label))

        super().__init__(id="resource_tools", tools=[Tool(resource_echo, args_validator=validate)])

    async def __aenter__(self) -> Self:
        assert activity.in_activity(), "Resource opened in Workflow"
        events.append(("enter", self.resource_label))
        self.entered = True
        await super().__aenter__()
        return self

    async def __aexit__(self, *args) -> None:
        try:
            await super().__aexit__(*args)
        finally:
            events.append(("exit", self.resource_label))
            self.entered = False

    async def get_tools(self, ctx: RunContext[PluginDeps]):
        assert activity.in_activity() and self.entered
        return await super().get_tools(ctx)


class ResourceCapability(Capability[PluginDeps]):
    def __init__(self, config: ResourceConfig) -> None:
        self.label = config.label
        super().__init__(
            id="resource",
            instructions=f"resource={config.label}",
            toolsets=[ResourceToolset(config.label)],
        )

    async def after_model_request(
        self,
        ctx: RunContext[PluginDeps],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        assert workflow.in_workflow(), "Hook escaped Workflow"
        if response.text:
            return replace(response, parts=[TextPart(f"{response.text}|hook:{self.label}")])
        return response

    async def after_tool_execute(
        self,
        ctx: RunContext[PluginDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        assert workflow.in_workflow(), "Tool hook escaped Workflow"
        assert ctx.capability_active is True
        assert (
            tool_def.capability_id
            == {
                "example_echo": "echo",
                "resource_echo": "resource",
            }[call.tool_name]
        )
        return f"{result}|tool-hook:{self.label}"


def build_resource(config: ResourceConfig) -> AbstractCapability[PluginDeps]:
    return ResourceCapability(config)


def build_nested_echo(config: EchoConfig) -> AbstractCapability[PluginDeps]:
    return CombinedCapability(capabilities=[CombinedCapability(capabilities=[build_echo(config)])])


async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    assert activity.in_activity(), "Model escaped Activity"
    returns = [
        part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)
    ]
    if info.function_tools and not returns:
        return ModelResponse(
            parts=[ToolCallPart(tool.name, {"text": "hello"}) for tool in info.function_tools]
        )
    return ModelResponse(
        parts=[TextPart(";".join(str(part.content) for part in returns) or "disabled")]
    )


registry = PluginRegistry[PluginDeps]()
registry.register("echo", config_type=EchoConfig, factory=build_nested_echo)
registry.register("resource", config_type=ResourceConfig, factory=build_resource)
agent = Agent(
    FunctionModel(model),
    name="capability_plugins_test",
    deps_type=PluginDeps,
    capabilities=[
        registry.capability(),
        TemporalDurability(
            activity_config={
                "start_to_close_timeout": timedelta(seconds=15),
                "retry_policy": RetryPolicy(maximum_attempts=1),
            }
        ),
    ],
)


async def approval_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    assert activity.in_activity(), "Model escaped Activity"
    (definition,) = info.function_tools
    return ModelResponse(
        parts=[
            TextPart(
                json.dumps(
                    {
                        "name": definition.name,
                        # SDK ToolDefinition carries requires_approval as kind.
                        "requires_approval": definition.kind == "unapproved",
                        "metadata": definition.metadata,
                        "capability_id": definition.capability_id,
                        "strict": definition.strict,
                        "sequential": definition.sequential,
                    }
                )
            )
        ]
    )


approval_registry = PluginRegistry[PluginDeps]()
approval_registry.register("approval", config_type=EchoConfig, factory=build_approval)
approval_agent = Agent(
    FunctionModel(approval_model),
    name="capability_plugins_approval",
    deps_type=PluginDeps,
    output_type=str | DeferredToolRequests,
    capabilities=[
        approval_registry.capability(),
        TemporalDurability(
            activity_config={
                "start_to_close_timeout": timedelta(seconds=15),
                "retry_policy": RetryPolicy(maximum_attempts=1),
            }
        ),
    ],
)

"""Temporal test fixture; resource operations assert that they stay in Activities."""

import json
from dataclasses import replace
from datetime import timedelta
from types import ModuleType
from typing import Any, Self

from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import Capability, CombinedCapability
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests, ToolDefinition
from pydantic_ai.toolsets import FunctionToolset, WrapperToolset
from temporalio import activity, workflow
from temporalio.common import RetryPolicy

from kapy.layers import LayerDeps, layers_capability
from kapy.layers.examples import echo
from kapy.layers.examples.echo import State
from layers import tool_definitions

events: list[tuple[str, str]] = []


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str


class ResourceToolset(FunctionToolset[LayerDeps]):
    def __init__(self, name: str) -> None:
        self.layer_name = name
        self.resource_label = ""
        self.entered = False

        async def resource_echo(ctx: RunContext[LayerDeps], text: str) -> str:
            label = Config.model_validate(ctx.deps.layers[name]["config"]).label
            assert activity.in_activity() and self.entered
            events.append(("call", label))
            return f"resource:{label}:{text}"

        async def validate(ctx: RunContext[LayerDeps], text: str) -> None:
            assert activity.in_activity() and self.entered
            assert text == "hello"
            events.append(
                ("validate", Config.model_validate(ctx.deps.layers[name]["config"]).label)
            )

        resource_tool = Tool[LayerDeps](resource_echo, takes_ctx=True)
        resource_tool.args_validator = validate
        super().__init__(id="resource_tools", tools=[resource_tool])

    async def for_run(self, ctx: RunContext[LayerDeps]):
        self.resource_label = Config.model_validate(
            ctx.deps.layers[self.layer_name]["config"]
        ).label
        return await super().for_run(ctx)

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

    async def get_tools(self, ctx: RunContext[LayerDeps]):
        assert activity.in_activity() and self.entered
        return await super().get_tools(ctx)


class ResourceCapability(Capability[LayerDeps]):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            id=name,
            instructions=self.layer_instructions,
            toolsets=[ResourceToolset(name)],
        )

    def label(self, ctx: RunContext[LayerDeps]) -> str:
        return Config.model_validate(ctx.deps.layers[self.name]["config"]).label

    def layer_instructions(self, ctx: RunContext[LayerDeps]) -> str:
        return f"resource={self.label(ctx)}"

    async def after_model_request(
        self,
        ctx: RunContext[LayerDeps],
        *,
        request_context: ModelRequestContext,
        response: ModelResponse,
    ) -> ModelResponse:
        assert workflow.in_workflow(), "Hook escaped Workflow"
        if response.text:
            return replace(response, parts=[TextPart(f"{response.text}|hook:{self.label(ctx)}")])
        return response

    async def after_tool_execute(
        self,
        ctx: RunContext[LayerDeps],
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
                "example_echo": "echo.tool",
                "resource_echo": "resource",
            }[call.tool_name]
        )
        return f"{result}|tool-hook:{self.label(ctx)}"


resource = ModuleType("resource")
for field, value in {"Config": Config, "State": State, "Capability": ResourceCapability}.items():
    setattr(resource, field, value)


class NestedEcho(CombinedCapability[LayerDeps]):
    def __init__(self, name: str) -> None:
        self.name = name
        child = echo.Capability(name)
        child.id = f"{name}.tool"
        super().__init__(id=name, capabilities=[CombinedCapability(capabilities=[child])])


nested_echo = ModuleType("nested_echo")
for field, value in {"Config": echo.Config, "State": echo.State, "Capability": NestedEcho}.items():
    setattr(nested_echo, field, value)


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


registry = {"echo": nested_echo, "resource": resource}
agent = Agent(
    FunctionModel(model),
    name="layers_test",
    deps_type=LayerDeps,
    capabilities=[
        layers_capability(registry),
        TemporalDurability(
            activity_config={
                "start_to_close_timeout": timedelta(seconds=15),
                "retry_policy": RetryPolicy(maximum_attempts=1),
            }
        ),
    ],
)

wrapped_agent = Agent(
    FunctionModel(model),
    name="layers_wrapped_test",
    deps_type=LayerDeps,
    capabilities=[
        layers_capability(registry, wrap_toolset=lambda name, toolset: WrapperToolset(toolset)),
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


approval_registry = {"approval": tool_definitions}
approval_agent = Agent(
    FunctionModel(approval_model),
    name="layers_approval",
    deps_type=LayerDeps,
    output_type=str | DeferredToolRequests,
    capabilities=[
        layers_capability(approval_registry),
        TemporalDurability(
            activity_config={
                "start_to_close_timeout": timedelta(seconds=15),
                "retry_policy": RetryPolicy(maximum_attempts=1),
            }
        ),
    ],
)

"""Temporal test fixture; resource operations assert that they stay in Activities."""

import json
from dataclasses import replace
from datetime import timedelta
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

from kapy.layers import Layer, LayerAccess, LayerData, LayerDeps, layers_capability, make_layers
from kapy.layers.examples import echo
from kapy.layers.examples.echo import State
from layers import tool_definitions

events: list[tuple[str, str]] = []


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str


class ResourceToolset(FunctionToolset[LayerDeps]):
    def __init__(self, access: LayerAccess[LayerDeps]) -> None:
        self.access = access
        self.resource_label = ""
        self.entered = False

        async def resource_echo(ctx: RunContext[LayerDeps], text: str) -> str:
            label = Config.model_validate(access(ctx.deps)["config"]).label
            assert activity.in_activity() and self.entered
            events.append(("call", label))
            return f"resource:{label}:{text}"

        async def validate(ctx: RunContext[LayerDeps], text: str) -> None:
            assert activity.in_activity() and self.entered
            assert text == "hello"
            events.append(("validate", Config.model_validate(access(ctx.deps)["config"]).label))

        resource_tool = Tool[LayerDeps](resource_echo, takes_ctx=True)
        resource_tool.args_validator = validate
        super().__init__(id="resource_tools", tools=[resource_tool])

    async def for_run(self, ctx: RunContext[LayerDeps]):
        # This definition is shared; each SDK binding owns its client/resource flags.
        toolset = ResourceToolset(self.access)
        toolset.resource_label = Config.model_validate(self.access(ctx.deps)["config"]).label
        return toolset

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
    def __init__(self, layer_id: str, access: LayerAccess[LayerDeps]) -> None:
        self.access = access
        super().__init__(
            id=layer_id,
            instructions=self.layer_instructions,
            toolsets=[ResourceToolset(access)],
        )

    def label(self, ctx: RunContext[LayerDeps]) -> str:
        return Config.model_validate(self.access(ctx.deps)["config"]).label

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


def resource(layer_id: str, access: LayerAccess[LayerDeps]) -> Layer[LayerDeps, Config, State]:
    return Layer(
        config_type=Config,
        state_type=State,
        capability=ResourceCapability(layer_id, access),
    )


def nested_echo(
    layer_id: str, access: LayerAccess[LayerDeps]
) -> Layer[LayerDeps, echo.Config, echo.State]:
    child = echo.make(f"{layer_id}.tool", access).capability
    return Layer(
        config_type=echo.Config,
        state_type=echo.State,
        capability=CombinedCapability(
            id=layer_id, capabilities=[CombinedCapability(capabilities=[child])]
        ),
    )


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


def get_layers(deps: LayerDeps) -> dict[str, LayerData]:
    return deps.layers


layers = make_layers(registry, get_layers)
agent = Agent(
    FunctionModel(model),
    name="layers_test",
    deps_type=LayerDeps,
    capabilities=[
        layers_capability(layers, get_layers=get_layers),
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
        layers_capability(
            layers,
            get_layers=get_layers,
            wrap_toolset=lambda name, toolset: WrapperToolset(toolset),
        ),
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


approval_layers = make_layers({"approval": tool_definitions.make}, get_layers)
approval_agent = Agent(
    FunctionModel(approval_model),
    name="layers_approval",
    deps_type=LayerDeps,
    output_type=str | DeferredToolRequests,
    capabilities=[
        layers_capability(approval_layers, get_layers=get_layers),
        TemporalDurability(
            activity_config={
                "start_to_close_timeout": timedelta(seconds=15),
                "retry_policy": RetryPolicy(maximum_attempts=1),
            }
        ),
    ],
)

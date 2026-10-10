"""Native combined layer: child ownership differs from persisted layer ownership."""

from dataclasses import replace
from typing import Any
from uuid import UUID

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability as NativeCapability
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.messages import ToolCallPart, ToolReturn
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from temporalio import workflow

from kapy.layers import Layer, LayerAccess, LayerData


class Deps(BaseModel):
    """An independent consumer stores layer data outside Kapy's deps.layers."""

    session_id: UUID
    payload: dict[str, LayerData]


def get_layers(deps: Deps) -> dict[str, LayerData]:
    return deps.payload


class Config(BaseModel):
    prefix: str = "nested:"


class State(BaseModel):
    calls: int = 0
    hooks: int = 0


class Counter(NativeCapability[Deps]):
    def __init__(self, layer_id: str, access: LayerAccess[Deps]) -> None:
        self.access = access
        super().__init__(id=f"{layer_id}.tool", tools=[self.echo])

    def echo(self, ctx: RunContext[Deps], text: str) -> Any:
        entry = self.access(ctx.deps)
        state = State.model_validate(entry["state"])
        state.calls += 1
        entry["state"] = state.model_dump(mode="json")
        value = f"{Config.model_validate(entry['config']).prefix}{state.calls}:{text}"
        if text == "B":
            return ToolReturn(
                value,
                content="native-extra",
                metadata=["application", {"state": 123}],
                tools=["echo"],
            )
        if text == "C":
            return {"kind": "tool-return", "value": value}
        return value

    async def after_tool_execute(
        self,
        ctx: RunContext[Deps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        if tool_def.capability_id != self.id:
            return result
        assert workflow.in_workflow() and ctx.capability_active is True
        state = State.model_validate(self.access(ctx.deps)["state"])
        assert state.calls == state.hooks + 1
        state.hooks += 1
        self.access(ctx.deps)["state"] = state.model_dump(mode="json")
        if args["text"] == "A":
            get_layers(ctx.deps)["other"]["state"] = {"calls": 10}
        else:
            assert get_layers(ctx.deps)["other"]["state"] == {"calls": 10}
        if args["text"] == "B":
            assert isinstance(result, ToolReturn)
            assert result.content == "native-extra" and result.tools == ["echo"]
            assert result.metadata == ["application", {"state": 123}]
            return replace(result, return_value=f"{result.return_value}|hook:{state.hooks}")
        if args["text"] == "C":
            assert result == {"kind": "tool-return", "value": "nested:3:C"}
            result = result["value"]
        return f"{result}|hook:{state.hooks}"


class Capability(CombinedCapability[Deps]):
    def __init__(self, layer_id: str, access: LayerAccess[Deps]) -> None:

        def lazy_echo() -> str:
            return "lazy"

        super().__init__(
            id=layer_id,
            capabilities=[
                Counter(layer_id, access),
                NativeCapability(
                    id=f"{layer_id}.lazy",
                    defer_loading=True,
                    instructions="LAZY_CAPABILITY_RULE",
                    toolsets=[
                        FunctionToolset(
                            tools=[lazy_echo], instructions="LAZY_TOOLSET_RULE", id="lazy-tools"
                        )
                    ],
                ),
            ],
        )

    def get_instructions(self):
        return "CONTAINER_RULE"


def make(layer_id: str, access: LayerAccess[Deps]) -> Layer[Deps, Config, State]:
    return Layer(config_type=Config, state_type=State, capability=Capability(layer_id, access))

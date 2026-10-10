"""Native combined layer: child ownership differs from persisted layer ownership."""

from dataclasses import replace
from typing import Any

from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability as NativeCapability
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.messages import ToolCallPart, ToolReturn
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset
from temporalio import workflow

from kapy.runner_duarable.types import RunnerDeps


class Config(BaseModel):
    prefix: str = "nested:"


class State(BaseModel):
    calls: int = 0
    hooks: int = 0


class Counter(NativeCapability[RunnerDeps]):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(id=f"{name}.tool", tools=[self.echo])

    def echo(self, ctx: RunContext[RunnerDeps], text: str) -> Any:
        entry = ctx.deps.layers[self.name]
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
        ctx: RunContext[RunnerDeps],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: dict[str, Any],
        result: Any,
    ) -> Any:
        if tool_def.capability_id != self.id:
            return result
        assert workflow.in_workflow() and ctx.capability_active is True
        state = State.model_validate(ctx.deps.layers[self.name]["state"])
        assert state.calls == state.hooks + 1
        state.hooks += 1
        ctx.deps.layers[self.name]["state"] = state.model_dump(mode="json")
        if args["text"] == "A":
            ctx.deps.layers["other"]["state"] = {"calls": 10}
        else:
            assert ctx.deps.layers["other"]["state"] == {"calls": 10}
        if args["text"] == "B":
            assert isinstance(result, ToolReturn)
            assert result.content == "native-extra" and result.tools == ["echo"]
            assert result.metadata == ["application", {"state": 123}]
            return replace(result, return_value=f"{result.return_value}|hook:{state.hooks}")
        if args["text"] == "C":
            assert result == {"kind": "tool-return", "value": "nested:3:C"}
            result = result["value"]
        return f"{result}|hook:{state.hooks}"


class Capability(CombinedCapability[RunnerDeps]):
    def __init__(self, name: str) -> None:
        self.name = name

        def lazy_echo() -> str:
            return "lazy"

        super().__init__(
            id=name,
            capabilities=[
                Counter(name),
                NativeCapability(
                    id=f"{name}.lazy",
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

"""The same deferred plugin and real SDK loading calls for native/registry comparisons."""

import json

from pydantic_ai.capabilities import Capability as NativeCapability
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.toolsets import FunctionToolset

from kapy.layers import LayerDeps
from kapy.layers.examples.echo import Config, State

__all__ = ["Config", "State", "Capability"]


class LazyToolset(FunctionToolset[LayerDeps]):
    def __init__(self) -> None:
        def lazy_tool() -> str:
            return "lazy-result"

        super().__init__(id="lazy-tools", tools=[lazy_tool], instructions="TOOLSET_RULE")


class Capability(NativeCapability[LayerDeps]):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            id=name,
            defer_loading=True,
            instructions="CAPABILITY_RULE",
            toolsets=[LazyToolset()],
        )


async def lazy_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returns = [
        part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)
    ]
    names = {tool.name for tool in info.function_tools}
    if not returns:
        assert "load_capability" in names and "lazy_tool" not in names
        return ModelResponse(parts=[ToolCallPart("load_capability", {"id": "lazy"})])
    if returns[-1].tool_name == "load_capability":
        assert "lazy_tool" in names
        return ModelResponse(parts=[ToolCallPart("lazy_tool", {})])
    assert returns[-1].tool_name == "lazy_tool"
    return ModelResponse(
        parts=[
            TextPart(
                json.dumps(
                    {
                        "instructions": returns[0].content,
                        "tool_result": returns[-1].content,
                    }
                )
            )
        ]
    )

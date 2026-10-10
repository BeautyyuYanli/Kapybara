"""The same deferred plugin and real SDK loading calls for native/static-layer comparisons."""

import json

from pydantic_ai.capabilities import Capability as NativeCapability
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo
from pydantic_ai.toolsets import FunctionToolset

from kapy.layers import Layer, LayerAccess
from kapy.layers.examples.echo import Config, State

__all__ = ["Capability", "lazy_model", "make"]


class LazyToolset[D](FunctionToolset[D]):
    def __init__(self) -> None:
        def lazy_tool() -> str:
            return "lazy-result"

        super().__init__(id="lazy-tools", tools=[lazy_tool], instructions="TOOLSET_RULE")


class Capability[D](NativeCapability[D]):
    def __init__(self, name: str) -> None:
        super().__init__(
            id=name,
            defer_loading=True,
            instructions="CAPABILITY_RULE",
            toolsets=[LazyToolset[D]()],
        )


def make[D](layer_id: str, access: LayerAccess[D]) -> Layer[D, Config, State]:
    return Layer(config_type=Config, state_type=State, capability=Capability[D](layer_id))


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

"""One nondefault tool definition shared by in-process and Temporal contracts."""

from dataclasses import replace

from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability as NativeCapability
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset, PreparedToolset

from pydantic_ai_layer_protocol import Layer, LayerAccess
from pydantic_ai_layer_protocol.examples.echo import Config, State

__all__ = ["make"]


def make[D](layer_id: str, access: LayerAccess[D]) -> Layer[D, Config, State]:
    def approved_echo(text: str) -> str:
        return text

    def prepare(ctx: RunContext[D], defs: list[ToolDefinition]) -> list[ToolDefinition]:
        return [replace(tool_def, capability_id="declared_owner") for tool_def in defs]

    toolset = PreparedToolset[D](
        wrapped=FunctionToolset[D](
            id="approval-tools",
            tools=[
                Tool(
                    approved_echo,
                    requires_approval=True,
                    metadata={"category": "example"},
                    strict=True,
                    sequential=True,
                )
            ],
        ),
        prepare_func=prepare,
    )
    return Layer(
        config_type=Config,
        state_type=State,
        capability=CombinedCapability(
            id=layer_id,
            capabilities=[
                NativeCapability(id=layer_id, toolsets=[toolset]),
                NativeCapability(id="declared_owner"),
            ],
        ),
    )

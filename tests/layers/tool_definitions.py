"""One nondefault tool definition shared by in-process and Temporal contracts."""

from dataclasses import replace

from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability as NativeCapability
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset, PreparedToolset

from kapy.layers import LayerDeps
from kapy.layers.examples.echo import Config, State

__all__ = ["Config", "State", "Capability"]


class Capability(CombinedCapability[LayerDeps]):
    def __init__(self, name: str) -> None:
        def approved_echo(text: str) -> str:
            return text

        def prepare(ctx: RunContext[LayerDeps], defs: list[ToolDefinition]) -> list[ToolDefinition]:
            return [replace(tool_def, capability_id="declared_owner") for tool_def in defs]

        toolset = PreparedToolset[LayerDeps](
            wrapped=FunctionToolset[LayerDeps](
                tools=[
                    Tool(
                        approved_echo,
                        requires_approval=True,
                        metadata={"category": "example"},
                        strict=True,
                        sequential=True,
                    )
                ]
            ),
            prepare_func=prepare,
        )
        super().__init__(
            capabilities=[
                NativeCapability(id=name, toolsets=[toolset]),
                NativeCapability(id="declared_owner"),
            ]
        )

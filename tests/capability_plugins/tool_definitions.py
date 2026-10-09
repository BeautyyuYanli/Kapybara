"""One nondefault tool definition shared by in-process and Temporal contracts."""

from dataclasses import replace

from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import AbstractCapability, Capability, CombinedCapability
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import FunctionToolset, PreparedToolset

from kapy.capability_plugins import PluginDeps
from kapy.capability_plugins.examples.echo import EchoConfig


def build_approval(config: EchoConfig) -> AbstractCapability[PluginDeps]:
    def approved_echo(text: str) -> str:
        return text

    def prepare(ctx: RunContext[PluginDeps], defs: list[ToolDefinition]) -> list[ToolDefinition]:
        return [replace(tool_def, capability_id="declared_owner") for tool_def in defs]

    toolset = PreparedToolset[PluginDeps](
        wrapped=FunctionToolset[PluginDeps](
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
    return CombinedCapability(
        capabilities=[
            Capability(id="approval", toolsets=[toolset]),
            Capability(id="declared_owner"),
        ]
    )

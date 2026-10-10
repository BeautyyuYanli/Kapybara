"""Return successful Activity State through native ToolReturn metadata.

SDK Activities serialize deps independently. The inner wrapper carries only the
called layer's validated State on success; the outer Workflow wrapper applies it
before business hooks and restores the original tool result. SDK retry/failure
signals bypass this success path. Temporal history records the transport, while
messages and models see no transport metadata. Database snapshots remain run-owned.
"""

from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.durable_exec.temporal import TemporalRunContext
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool, WrapperToolset
from temporalio import workflow

from kapy.layers.modules import dump_object, layer_types
from kapy.layers.registry import REGISTRY

from .types import RunnerDeps

_OWNER = "kapy_layer"


@dataclass
class _ReturnLayerStateToolset(WrapperToolset[RunnerDeps]):
    name: str

    async def get_tools(self, ctx: RunContext[RunnerDeps]) -> dict[str, ToolsetTool[RunnerDeps]]:
        return {
            name: replace(
                tool,
                tool_def=replace(
                    tool.tool_def,
                    metadata=(tool.tool_def.metadata or {}) | {_OWNER: self.name},
                    # Each tool can mutate the same JSON inventory. Sequential
                    # dispatch applies returned State before serializing the next call.
                    sequential=True,
                ),
            )
            for name, tool in (await self.wrapped.get_tools(ctx)).items()
        }

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[RunnerDeps],
        tool: ToolsetTool[RunnerDeps],
    ) -> Any:
        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
        if not isinstance(ctx, TemporalRunContext):
            return result
        state = dump_object(
            layer_types(self.name, REGISTRY)[1].model_validate(ctx.deps.layers[self.name]["state"])
        )
        native_return = isinstance(result, ToolReturn)
        metadata = {
            "state": state,
            "native_return": native_return,
            "metadata": result.metadata if native_return else None,
        }
        # Replacing the transport metadata also preserves arbitrary native
        # metadata without reserving a key in the layer's public return value.
        if native_return:
            return replace(result, metadata=metadata)
        return ToolReturn(result, metadata=metadata)


def return_layer_state(
    name: str, toolset: AbstractToolset[RunnerDeps]
) -> AbstractToolset[RunnerDeps]:
    return _ReturnLayerStateToolset(toolset, name=name)


class _ApplyLayerStateToolset(WrapperToolset[RunnerDeps]):
    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[RunnerDeps],
        tool: ToolsetTool[RunnerDeps],
    ) -> Any:
        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
        owner = (tool.tool_def.metadata or {}).get(_OWNER)
        if not workflow.in_workflow() or owner is None:
            return result
        assert isinstance(result, ToolReturn)
        transport = result.metadata
        ctx.deps.layers[owner]["state"] = transport["state"]
        if transport["native_return"]:
            return replace(result, metadata=transport["metadata"])
        return result.return_value


class LayerStateCapability(AbstractCapability[RunnerDeps]):
    """Apply successful State before native business hooks see the original result."""

    def get_wrapper_toolset(
        self, toolset: AbstractToolset[RunnerDeps]
    ) -> AbstractToolset[RunnerDeps]:
        return _ApplyLayerStateToolset(toolset)

"""Return successful Activity State through native ToolReturn metadata.

SDK Activities serialize deps independently. The inner wrapper carries only the
called layer's validated State on success; the outer Workflow wrapper applies it
before business hooks and restores the original tool result. SDK retry/failure
signals bypass this success path. Temporal history records the transport, while
messages and models see no transport metadata. Database snapshots remain run-owned.
Both directions use the same injected
writable deps mapping; the inner wrapper captures identity and model type from
the selected static Layer, without a global registry lookup.
"""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.durable_exec.temporal import TemporalRunContext
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool, WrapperToolset
from temporalio import workflow

from kapy.control.types import DTO
from kapy.layers.modules import dump_object
from kapy.layers.types import JsonObject, Layer, LayerData

_OWNER = "kapy_layer"


class LayerStateMetadata(DTO):
    """Validate the State transport while preserving arbitrary native metadata.

    All fields are required; dump to a dictionary to retain the recorded wire
    format. The Workflow validates this envelope before changing its State.
    """

    state: JsonObject
    native_return: bool = Field(strict=True)
    metadata: Any


@dataclass
class _ReturnLayerStateToolset[D](WrapperToolset[D]):
    layer_id: str
    state_type: type[BaseModel]
    get_layers: Callable[[D], dict[str, LayerData]]

    async def get_tools(self, ctx: RunContext[D]) -> dict[str, ToolsetTool[D]]:
        return {
            name: replace(
                tool,
                tool_def=replace(
                    tool.tool_def,
                    metadata=(tool.tool_def.metadata or {}) | {_OWNER: self.layer_id},
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
        ctx: RunContext[D],
        tool: ToolsetTool[D],
    ) -> Any:
        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
        if not isinstance(ctx, TemporalRunContext):
            return result
        state = dump_object(
            self.state_type.model_validate(
                deepcopy(self.get_layers(ctx.deps)[self.layer_id]["state"])
            )
        )
        native_return = isinstance(result, ToolReturn)
        metadata = LayerStateMetadata(
            state=state,
            native_return=native_return,
            metadata=result.metadata if native_return else None,
        ).model_dump()
        # Replacing the transport metadata also preserves arbitrary native
        # metadata without reserving a key in the layer's public return value.
        if native_return:
            return replace(result, metadata=metadata)
        return ToolReturn(result, metadata=metadata)


def return_layer_state[D](
    layer: Layer[D, Any, Any],
    toolset: AbstractToolset[D],
    *,
    get_layers: Callable[[D], dict[str, LayerData]],
) -> AbstractToolset[D]:
    """Capture this static layer's identity/models and the same writable deps rule."""
    return _ReturnLayerStateToolset(
        toolset, layer_id=layer.id, state_type=layer.state_type, get_layers=get_layers
    )


@dataclass
class _ApplyLayerStateToolset[D](WrapperToolset[D]):
    get_layers: Callable[[D], dict[str, LayerData]]

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[D],
        tool: ToolsetTool[D],
    ) -> Any:
        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
        owner = (tool.tool_def.metadata or {}).get(_OWNER)
        if not workflow.in_workflow() or owner is None:
            return result
        assert isinstance(result, ToolReturn)
        transport = LayerStateMetadata.model_validate(result.metadata)
        self.get_layers(ctx.deps)[owner]["state"] = transport.state
        if transport.native_return:
            return replace(result, metadata=transport.metadata)
        return result.return_value


@dataclass
class LayerStateCapability[D](AbstractCapability[D]):
    """Apply successful State before native business hooks see the original result."""

    get_layers: Callable[[D], dict[str, LayerData]]

    def get_wrapper_toolset(self, toolset: AbstractToolset[D]) -> AbstractToolset[D]:
        return _ApplyLayerStateToolset(toolset, get_layers=self.get_layers)

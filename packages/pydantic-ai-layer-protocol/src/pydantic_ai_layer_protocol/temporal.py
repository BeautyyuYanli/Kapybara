"""Return successful Activity State through native ToolReturn metadata.

SDK Activities serialize deps independently. The inner wrapper carries only the
called layer's validated State on success; the outer Workflow wrapper applies it
before business hooks and restores the original tool result. SDK retry/failure
signals bypass this success path. Temporal history records the transport, while
messages and models see no transport metadata. Hosts own persistence. Both
directions use the same injected writable deps mapping; the inner wrapper captures
identity and model type from the selected static Layer, without a global registry lookup.
``TemporalLayerRegistry`` assembles both directions with layer selection;
hosts supply only their installed factories and writable deps mapping rule.
"""

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, CombinedCapability
from pydantic_ai.durable_exec.temporal import TemporalRunContext
from pydantic_ai.messages import ToolReturn
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool, WrapperToolset
from temporalio import workflow

from .composition import _layers_capability
from .registry import LayerRegistry, dump_object
from .types import JsonObject, Layer, LayerData

LAYER_STATE_OWNER_KEY = "pydantic_ai_layer"
"""Tool-definition metadata key identifying the layer whose State is transported."""


class LayerStateMetadata(BaseModel):
    """Validate the State transport while preserving arbitrary native metadata.

    All fields are required; dump to a dictionary to retain the recorded wire
    format. The Workflow validates this envelope before changing its State.
    """

    model_config = ConfigDict(extra="forbid", from_attributes=True)

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
                    metadata=(tool.tool_def.metadata or {})
                    | {LAYER_STATE_OWNER_KEY: self.layer_id},
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
        owner = (tool.tool_def.metadata or {}).get(LAYER_STATE_OWNER_KEY)
        if not workflow.in_workflow() or owner is None:
            return result
        assert isinstance(result, ToolReturn)
        transport = LayerStateMetadata.model_validate(result.metadata)
        self.get_layers(ctx.deps)[owner]["state"] = transport.state
        if transport.native_return:
            return replace(result, metadata=transport.metadata)
        return result.return_value


@dataclass
class _LayerStateCapability[D](AbstractCapability[D]):
    """Apply successful State before native business hooks see the original result."""

    get_layers: Callable[[D], dict[str, LayerData]]

    def get_wrapper_toolset(self, toolset: AbstractToolset[D]) -> AbstractToolset[D]:
        return _ApplyLayerStateToolset(toolset, get_layers=self.get_layers)


class TemporalLayerRegistry[D](LayerRegistry[D]):
    """Install layer selection, Activity State return and Workflow State application.

    Install alongside the SDK's TemporalDurability. Both transport directions
    share the same access rule, so hosts cannot accidentally install just one.
    Tools use LAYER_STATE_OWNER_KEY and run sequentially. Without a Workflow,
    wrappers preserve original results and tools directly update writable deps.
    """

    def _build_capability(self) -> AbstractCapability[D]:
        def wrap_toolset(
            layer: Layer[D, Any, Any], toolset: AbstractToolset[D]
        ) -> AbstractToolset[D]:
            return _ReturnLayerStateToolset(
                toolset, layer_id=layer.id, state_type=layer.state_type, get_layers=self._get_layers
            )

        return CombinedCapability(
            [
                _layers_capability(
                    self.definitions, get_layers=self._get_layers, wrap_toolset=wrap_toolset
                ),
                _LayerStateCapability(get_layers=self._get_layers),
            ]
        )

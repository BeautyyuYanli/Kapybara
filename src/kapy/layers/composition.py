"""One stable DynamicCapability for ordinary and Temporal layer composition.

Layer capabilities with toolsets declare stable IDs. Resolve ownership from
those IDs rather than the live registry omitted by Temporal Activity contexts.
The ownership wrapper remains SDK-native for deferred instruction collection.
Each resolution creates its own composition and binds selected static capabilities
and their toolsets; definitions never retain run data or business-scope resources.
"""

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    CombinedCapability,
    DynamicCapability,
    WrapperCapability,
)
from pydantic_ai.exceptions import UserError
from pydantic_ai.toolsets import (
    AbstractToolset,
    CombinedToolset,
    DynamicToolset,
    ToolsetTool,
    WrapperToolset,
)
from pydantic_ai.toolsets._capability_owned import CapabilityOwnedToolset

from .modules import load_layers
from .types import Layer, LayerData


class _OwnedToolset[DepsT](CapabilityOwnedToolset[DepsT]):
    """Use the declared owner ID; inherit native deferral and visitor behavior.

    get_tools copies SDK definition processing because ownership resolution has
    no override hook. tests/layers/test_sdk_contract.py guards that copied method
    and compares the patch with the installed SDK; upgrades require review.
    """

    async def get_tools(self, ctx: RunContext[DepsT]) -> dict[str, ToolsetTool[DepsT]]:
        capability_id = self.capability.id
        if not capability_id:
            raise UserError("Layer capabilities with toolsets must declare a nonempty stable id")
        tools = await self.wrapped.get_tools(ctx)
        return {
            name: replace(
                tool,
                tool_def=replace(
                    tool.tool_def,
                    capability_id=(
                        tool.tool_def.capability_id
                        if tool.tool_def.capability_id is not None
                        else capability_id
                    ),
                    defer_loading=self.capability.defer_loading is True
                    or tool.tool_def.defer_loading,
                ),
            )
            for name, tool in tools.items()
        }


def _patch_ownership[DepsT](
    toolset: AbstractToolset[DepsT],
) -> AbstractToolset[DepsT]:
    # A layer wrapper can hide a CombinedCapability from SDK flattening. Rewrite
    # every native ownership node, retaining its surrounding state/resource
    # wrappers and the original capability objects used by instructions/hooks.
    if isinstance(toolset, CapabilityOwnedToolset):
        return _OwnedToolset(_patch_ownership(toolset.wrapped), capability=toolset.capability)
    if isinstance(toolset, WrapperToolset):
        return replace(toolset, wrapped=_patch_ownership(toolset.wrapped))
    if isinstance(toolset, CombinedToolset):
        return replace(toolset, toolsets=[_patch_ownership(child) for child in toolset.toolsets])
    return toolset


class _LayerCapabilities[DepsT](CombinedCapability[DepsT]):
    def get_toolset(
        self,
    ) -> Callable[[RunContext[DepsT]], Awaitable[AbstractToolset[DepsT]]] | None:
        toolset = super().get_toolset()
        if toolset is None:
            return None
        if not isinstance(toolset, AbstractToolset):
            toolset = DynamicToolset[DepsT](toolset)
        owned = _patch_ownership(toolset)

        async def bind(ctx: RunContext[DepsT]) -> AbstractToolset[DepsT]:
            # SDK DynamicCapability binds capabilities before ctx.capabilities is
            # complete, and its DynamicToolset does not bind inner contributions.
            # This native callable contribution runs at tool resolution, where
            # ordinary runs have the finalized context, once per resolution. Each
            # reconstructed Activity binds its own copy through the same path.
            return await owned.for_run(ctx)

        return bind


@dataclass
class _WrappedLayer[DepsT](WrapperCapability[DepsT]):
    layer: Layer[DepsT, Any, Any]
    wrap_toolset: Callable[[Layer[DepsT, Any, Any], AbstractToolset[DepsT]], AbstractToolset[DepsT]]

    def get_toolset(self) -> AbstractToolset[DepsT] | None:
        toolset = self.wrapped.get_toolset()
        if toolset is None:
            return None
        return self.wrap_toolset(
            self.layer,
            toolset if isinstance(toolset, AbstractToolset) else DynamicToolset(toolset),
        )


def layers_capability[DepsT](
    layers: Mapping[str, Layer[DepsT, Any, Any]],
    *,
    get_layers: Callable[[DepsT], dict[str, LayerData]],
    wrap_toolset: Callable[[Layer[DepsT, Any, Any], AbstractToolset[DepsT]], AbstractToolset[DepsT]]
    | None = None,
) -> DynamicCapability[DepsT]:
    """Capture static definitions; select from current writable deps on resolution.

    The optional wrapper is for a consumer's Activity/state boundary. Native
    hooks, for_agent/for_run binding, instructions and resource lifecycle remain
    SDK-owned. Assembly and selection never call business-scope hooks.
    """
    installed = dict(layers)

    def resolve(ctx: RunContext[DepsT]) -> AbstractCapability[DepsT] | None:
        # Validate serialized direct runner inputs as well as session-created data.
        selected = get_layers(ctx.deps)
        try:
            load_layers(
                {name: data["config"] for name, data in selected.items()},
                {name: data["state"] for name, data in selected.items()},
                installed,
            )
        except ValueError as exc:
            raise UserError("Invalid layer configuration or state") from exc
        capabilities = []
        for name in selected:
            layer = installed[name]
            capability = layer.capability
            if wrap_toolset is not None:
                capability = _WrappedLayer(capability, layer=layer, wrap_toolset=wrap_toolset)
            capabilities.append(capability)
        if not capabilities:
            return None
        return _LayerCapabilities(capabilities=capabilities)

    return DynamicCapability(resolve, id="layers")

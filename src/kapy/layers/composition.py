"""One stable DynamicCapability for ordinary and Temporal layer composition.

Layer capabilities with toolsets declare stable IDs. Resolve ownership from
those IDs rather than the live registry omitted by Temporal Activity contexts.
The ownership wrapper remains SDK-native for deferred instruction collection.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from types import ModuleType

from pydantic_ai import RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    Capability,
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

from .modules import create_layers, load_layers
from .types import HasLayers


class _OwnedToolset[DepsT: HasLayers](CapabilityOwnedToolset[DepsT]):
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


def _patch_ownership[DepsT: HasLayers](
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


class _LayerCapabilities[DepsT: HasLayers](CombinedCapability[DepsT]):
    def get_toolset(self) -> AbstractToolset[DepsT] | None:
        toolset = super().get_toolset()
        if toolset is None:
            return None
        return _patch_ownership(
            toolset if isinstance(toolset, AbstractToolset) else DynamicToolset(toolset)
        )


@dataclass
class _WrappedLayer[DepsT: HasLayers](WrapperCapability[DepsT]):
    name: str
    wrap_toolset: Callable[[str, AbstractToolset[DepsT]], AbstractToolset[DepsT]]

    def get_toolset(self) -> AbstractToolset[DepsT] | None:
        toolset = self.wrapped.get_toolset()
        if toolset is None:
            return None
        return self.wrap_toolset(
            self.name,
            toolset if isinstance(toolset, AbstractToolset) else DynamicToolset(toolset),
        )


def layers_capability[DepsT: HasLayers](
    registry: Mapping[str, ModuleType],
    *,
    wrap_toolset: Callable[[str, AbstractToolset[DepsT]], AbstractToolset[DepsT]] | None = None,
) -> DynamicCapability[DepsT]:
    """Capture modules at Agent construction; a run selects through deps.layers.

    The optional wrapper is for a consumer's Activity/state boundary. Native
    hooks, instructions and Toolset resource lifecycle remain SDK-owned.
    """
    modules = dict(registry)

    def resolve(ctx: RunContext[DepsT]) -> AbstractCapability[DepsT] | None:
        # Validate serialized direct runner inputs as well as session-created data.
        try:
            load_layers(
                {name: data["config"] for name, data in ctx.deps.layers.items()},
                {name: data["state"] for name, data in ctx.deps.layers.items()},
                modules,
            )
        except ValueError as exc:
            raise UserError("Invalid layer configuration or state") from exc
        capabilities = []
        for name, component in create_layers(ctx.deps, modules).items():
            capability = (
                Capability(toolsets=[component], id=name)
                if isinstance(component, AbstractToolset)
                else component
            )
            if wrap_toolset is not None:
                capability = _WrappedLayer(capability, name=name, wrap_toolset=wrap_toolset)
            capabilities.append(capability)
        if not capabilities:
            return None
        return _LayerCapabilities(capabilities=capabilities)

    return DynamicCapability(resolve, id="layers")

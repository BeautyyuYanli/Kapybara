"""One stable DynamicCapability for ordinary and Temporal layer composition.

SDK 2.54's CombinedCapability ownership wrapper needs live ctx.capabilities,
which Temporal Activity contexts deliberately omit. Activity resolution uses a
small ownership wrapper while ordinary runs retain the native deferred loader.
"""

from collections.abc import Callable, Mapping, Sequence
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
from pydantic_ai.durable_exec.temporal import TemporalRunContext
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import InstructionPart
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


@dataclass
class _OwnedToolset[DepsT: HasLayers](WrapperToolset[DepsT]):
    capability: AbstractCapability[DepsT]

    async def get_tools(self, ctx: RunContext[DepsT]) -> dict[str, ToolsetTool[DepsT]]:
        tools = await self.wrapped.get_tools(ctx)
        if tools and self.capability.id is None:
            raise UserError("Layer capabilities with tools must declare a stable id")
        return {
            name: replace(
                tool,
                tool_def=replace(
                    tool.tool_def,
                    capability_id=tool.tool_def.capability_id or self.capability.id,
                    defer_loading=self.capability.defer_loading is True
                    or tool.tool_def.defer_loading,
                ),
            )
            for name, tool in tools.items()
        }

    async def get_instructions(
        self, ctx: RunContext[DepsT]
    ) -> str | InstructionPart | Sequence[str | InstructionPart] | None:
        if self.capability.defer_loading is True:
            return None
        return await super().get_instructions(ctx)


def _activity_ownership[DepsT: HasLayers](
    toolset: AbstractToolset[DepsT],
) -> AbstractToolset[DepsT]:
    # A layer wrapper can hide a CombinedCapability from SDK flattening. Rewrite
    # every native ownership node, retaining its surrounding state/resource
    # wrappers and the original capability objects used by instructions/hooks.
    if isinstance(toolset, CapabilityOwnedToolset):
        return _OwnedToolset(_activity_ownership(toolset.wrapped), capability=toolset.capability)
    if isinstance(toolset, WrapperToolset):
        return replace(toolset, wrapped=_activity_ownership(toolset.wrapped))
    if isinstance(toolset, CombinedToolset):
        return replace(toolset, toolsets=[_activity_ownership(child) for child in toolset.toolsets])
    return toolset


class _ActivityCapabilities[DepsT: HasLayers](CombinedCapability[DepsT]):
    def get_toolset(self) -> AbstractToolset[DepsT] | None:
        toolset = super().get_toolset()
        if toolset is None:
            return None
        return _activity_ownership(
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
        if isinstance(ctx, TemporalRunContext):
            return _ActivityCapabilities(capabilities=capabilities)
        return CombinedCapability(capabilities=capabilities)

    return DynamicCapability(resolve, id="layers")

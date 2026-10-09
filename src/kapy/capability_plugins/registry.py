"""Process-local definitions composed through one stable DynamicCapability.

Factories and config validators are trusted, deterministic construction code;
they must not perform I/O. Temporal re-runs them in each tool Activity using
serialized deps. Resource ownership stays with the SDK toolsets and consumer.
"""

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass, replace

from pydantic import BaseModel, JsonValue, ValidationError
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, CombinedCapability, DynamicCapability
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

from .types import PluginDeps


@dataclass
class _PluginToolset[DepsT: PluginDeps](WrapperToolset[DepsT]):
    """Keep ownership on tool definitions without requiring live run capabilities.

    SDK 2.54's CombinedCapability ownership wrapper looks up ``ctx.capabilities``.
    Temporal intentionally excludes those live objects from Activity contexts.
    Plugins instead declare stable capability IDs that survive serialization.
    Lifecycle and calls still delegate to the original SDK toolset.
    """

    capability: AbstractCapability[DepsT]

    async def get_tools(self, ctx: RunContext[DepsT]) -> dict[str, ToolsetTool[DepsT]]:
        tools = await self.wrapped.get_tools(ctx)
        if tools and self.capability.id is None:
            raise UserError("Plugin capabilities that contribute tools must declare a stable id")
        return {
            name: replace(
                tool,
                tool_def=replace(
                    tool.tool_def,
                    capability_id=tool.tool_def.capability_id
                    if tool.tool_def.capability_id is not None
                    else self.capability.id,
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


class _PluginCapabilities[DepsT: PluginDeps](CombinedCapability[DepsT]):
    """Activity-only ownership without a live capability registry.

    Ordinary/Workflow resolution uses native CombinedCapability. Ordinary runs
    retain the ownership wrappers the SDK deferred loader recognizes.
    """

    def get_toolset(self) -> AbstractToolset[DepsT] | None:
        toolsets: list[AbstractToolset[DepsT]] = []
        for capability in self.capabilities:
            toolset = capability.get_toolset()
            if toolset is not None:
                toolsets.append(
                    _PluginToolset(
                        wrapped=toolset
                        if isinstance(toolset, AbstractToolset)
                        else DynamicToolset(toolset),
                        capability=capability,
                    )
                )
        return CombinedToolset(toolsets) if toolsets else None


class PluginRegistry[DepsT: PluginDeps]:
    """Register plugin definitions before creating an Agent's dynamic entrance.

    Only registration mutates the registry. Each entrance captures a snapshot;
    later registrations cannot change an existing Agent's available plugins.
    Config objects and capabilities are rebuilt for each resolution, never kept
    as registry state or shared between runs.
    """

    def __init__(self) -> None:
        self._definitions: dict[str, Callable[[JsonValue], AbstractCapability[DepsT]]] = {}

    def register[ConfigT: BaseModel](
        self,
        plugin_name: str,
        *,
        config_type: type[ConfigT],
        factory: Callable[[ConfigT], AbstractCapability[DepsT]],
    ) -> None:
        """Register a pure synchronous factory; duplicate/empty names raise ValueError.

        Each call to the factory must construct a fresh capability. Its tool
        names and capability IDs are declared by the plugin, not this registry.
        """
        if not isinstance(plugin_name, str) or not plugin_name:
            raise ValueError("plugin_name must be a nonempty string")
        if plugin_name in self._definitions:
            raise ValueError(f"Plugin {plugin_name!r} is already registered")

        def build(config: JsonValue) -> AbstractCapability[DepsT]:
            try:
                parsed = config_type.model_validate(deepcopy(config))
            except ValidationError as exc:
                raise UserError(f"Invalid configuration for plugin {plugin_name!r}") from exc
            capability = factory(parsed)
            if not isinstance(capability, AbstractCapability):
                raise UserError(f"Plugin {plugin_name!r} factory must return AbstractCapability")
            return capability

        self._definitions[plugin_name] = build

    def capability(self, *, id: str = "plugins") -> DynamicCapability[DepsT]:
        """Capture definitions in a stable entrance installed at Agent construction.

        Each run selects plugins through ``ctx.deps.plugins``. Unknown or repeated
        names, invalid configs, and invalid factory returns raise SDK UserError,
        which the Temporal plugin classifies as an execution failure.
        """
        definitions = self._definitions.copy()

        def resolve(ctx: RunContext[DepsT]) -> AbstractCapability[DepsT] | None:
            selected: set[str] = set()
            for spec in ctx.deps.plugins:
                name = spec.plugin_name
                if name in selected:
                    raise UserError(f"Plugin {name!r} is selected more than once")
                if name not in definitions:
                    raise UserError(f"Plugin {name!r} is not registered")
                selected.add(name)

            capabilities = [definitions[spec.plugin_name](spec.config) for spec in ctx.deps.plugins]
            if not capabilities:
                return None
            if isinstance(ctx, TemporalRunContext):
                return _PluginCapabilities(capabilities=capabilities)
            return CombinedCapability(capabilities=capabilities)

        return DynamicCapability(resolve, id=id)

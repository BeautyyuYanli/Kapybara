"""Select static layers while native Dynamic capabilities own leaf Activities.

Composition and capability ownership stay outside Temporal Activities, whose
serialized contexts have no live capability registry. Registered leaves have
stable IDs and bind their Toolsets inside the SDK's native Activity boundary.
Static definitions retain no execution deps or business-scope resources.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.agent.abstract import AbstractAgent
from pydantic_ai.capabilities import (
    AbstractCapability,
    CapabilityOrdering,
    CombinedCapability,
    DynamicCapability,
    WrapperCapability,
)
from pydantic_ai.exceptions import UserError
from pydantic_ai.toolsets import AbstractToolset, AgentToolset, DynamicToolset

from .modules import load_layers
from .types import Layer, LayerData


@dataclass
class _LayerLeaf[DepsT](WrapperCapability[DepsT]):
    """Delegate capability behavior and bind its Toolset at tool resolution.

    Dynamic resolves the capability before ordinary runs finalize their registry.
    Its callable Toolset contribution runs later, with that complete context, or
    with an Activity's freshly reconstructed context and resources.
    """

    layer: Layer[DepsT, Any, Any]
    wrap_toolset: (
        Callable[[Layer[DepsT, Any, Any], AbstractToolset[DepsT]], AbstractToolset[DepsT]] | None
    )

    def get_ordering(self) -> CapabilityOrdering | None:
        # SDK WrapperCapability does not forward ordering; Combined re-sorts
        # resolved leaves using these constraints.
        return self.wrapped.get_ordering()

    def get_toolset(self) -> AgentToolset[DepsT] | None:
        contribution = self.wrapped.get_toolset()
        if contribution is None:
            return None
        toolset = (
            contribution
            if isinstance(contribution, AbstractToolset)
            else DynamicToolset(contribution)
        )
        if self.wrap_toolset is not None:
            toolset = self.wrap_toolset(self.layer, toolset)

        async def bind(ctx: RunContext[DepsT]) -> AbstractToolset[DepsT]:
            return await toolset.for_run(ctx)

        return bind


@dataclass
class _SelectedLayers[DepsT](AbstractCapability[DepsT]):
    """Expose all Activity entries, then replace selection with a run-local composition."""

    installed: Mapping[str, Layer[DepsT, Any, Any]]
    prepared: Mapping[str, AbstractCapability[DepsT]]
    get_layers: Callable[[DepsT], dict[str, LayerData]]

    def for_agent(self, agent: AbstractAgent[DepsT, Any]) -> AbstractCapability[DepsT]:
        return replace(
            self, prepared={name: cap.for_agent(agent) for name, cap in self.prepared.items()}
        )

    def get_toolset(self) -> AgentToolset[DepsT] | None:
        # Durable engines register the complete installed entry set at construction.
        return CombinedCapability(list(self.prepared.values())).get_toolset()

    async def for_run(self, ctx: RunContext[DepsT]) -> AbstractCapability[DepsT]:
        selected = self.get_layers(ctx.deps)
        try:
            load_layers(
                {name: data["config"] for name, data in selected.items()},
                {name: data["state"] for name, data in selected.items()},
                self.installed,
            )
        except ValueError as exc:
            raise UserError("Invalid layer configuration or state") from exc
        return await CombinedCapability([self.prepared[name] for name in selected]).for_run(ctx)


def layers_capability[DepsT](
    layers: Mapping[str, Layer[DepsT, Any, Any]],
    *,
    get_layers: Callable[[DepsT], dict[str, LayerData]],
    wrap_toolset: Callable[[Layer[DepsT, Any, Any], AbstractToolset[DepsT]], AbstractToolset[DepsT]]
    | None = None,
) -> AbstractCapability[DepsT]:
    """Prepare installed leaves once; validate and select current deps for each run.

    Registered leaves require nonempty stable capability IDs. Native containers
    retain their composition and instructions; membership stays fixed after
    assembly. Per-run Toolsets may be callable or dynamic, while capability
    factories producing unknown subtrees are outside this static contract.
    The optional wrapper runs on each leaf's contribution with its whole Layer.
    Assembly and selection never call business-scope hooks.
    """
    installed = dict(layers)
    prepared: dict[str, AbstractCapability[DepsT]] = {}
    for name, layer in installed.items():

        def convert(
            capability: AbstractCapability[DepsT], *, layer=layer
        ) -> AbstractCapability[DepsT]:
            registered: list[AbstractCapability[DepsT]] = []
            capability.apply(registered.append)
            if len(registered) != 1 or registered[0] is not capability:
                return capability
            if not capability.id:
                raise UserError("Layer capability leaves must declare a nonempty stable id")

            def resolve(ctx: RunContext[DepsT]) -> AbstractCapability[DepsT]:
                return _LayerLeaf(capability, layer=layer, wrap_toolset=wrap_toolset)

            return DynamicCapability(resolve, id=capability.id)

        capability = layer.capability.visit_and_replace(convert)
        assert capability is not None
        prepared[name] = capability
    return _SelectedLayers(installed=installed, prepared=prepared, get_layers=get_layers)

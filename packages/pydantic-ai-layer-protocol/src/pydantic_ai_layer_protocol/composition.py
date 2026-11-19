"""Select static layers and bind native capabilities and Toolsets to each run.

Static definitions retain no execution deps or runtime handles. Native SDK
for_agent/for_run hooks keep capability and Toolset lifecycles independent.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.agent.abstract import AbstractAgent
from pydantic_ai.capabilities import AbstractCapability, CombinedCapability
from pydantic_ai.exceptions import UserError

from .registry import _load
from .types import Layer, LayerData


@dataclass
class _SelectedLayers[DepsT](AbstractCapability[DepsT]):
    """Replace selection with a run-local composition of installed definitions."""

    installed: Mapping[str, Layer[DepsT, Any, Any, Any]]
    prepared: Mapping[str, AbstractCapability[DepsT]]
    get_layers: Callable[[DepsT], dict[str, LayerData]]
    get_resources: Callable[[DepsT], Mapping[str, Any]] | None

    def for_agent(self, agent: AbstractAgent[DepsT, Any]) -> AbstractCapability[DepsT]:
        return replace(
            self, prepared={name: cap.for_agent(agent) for name, cap in self.prepared.items()}
        )

    async def for_run(self, ctx: RunContext[DepsT]) -> AbstractCapability[DepsT]:
        selected = self.get_layers(ctx.deps)
        try:
            _load(
                {name: data["config"] for name, data in selected.items()},
                {name: data["state"] for name, data in selected.items()},
                self.installed,
            )
        except ValueError as exc:
            raise UserError("Invalid layer configuration or state") from exc
        resources = self.get_resources(ctx.deps) if self.get_resources is not None else {}
        for name in selected:
            if self.installed[name].enter_conversation is not None and name not in resources:
                raise UserError(f"Layer {name!r} has no open resource")
        return await CombinedCapability([self.prepared[name] for name in selected]).for_run(ctx)


def _layers_capability[DepsT](
    layers: Mapping[str, Layer[DepsT, Any, Any, Any]],
    *,
    get_layers: Callable[[DepsT], dict[str, LayerData]],
    get_resources: Callable[[DepsT], Mapping[str, Any]] | None,
) -> AbstractCapability[DepsT]:
    """Prepare stable installed leaves and select current deps without opening handles.

    Native containers retain their structure. Callable or dynamic leaf Toolsets
    are supported; capability factories producing unknown subtrees are outside
    the static installation contract.
    """
    installed = dict(layers)

    def validate_leaf(capability: AbstractCapability[DepsT]) -> None:
        if not capability.id:
            raise UserError("Layer capability leaves must declare a nonempty stable id")

    for layer in installed.values():
        layer.capability.apply(validate_leaf)
    return _SelectedLayers(
        installed=installed,
        prepared={name: layer.capability for name, layer in installed.items()},
        get_layers=get_layers,
        get_resources=get_resources,
    )

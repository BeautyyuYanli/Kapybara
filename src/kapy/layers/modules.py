"""Allowlisted module construction and explicit JSON state snapshots.

Modules export Config, State and exactly one native Capability or Toolset.
Validators and constructors must be deterministic and perform no I/O; Temporal
reconstructs these components from the same deps in every tool Activity.
"""

from collections.abc import Callable, Mapping
from copy import deepcopy
from types import ModuleType
from typing import Any, cast

from pydantic import BaseModel
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.toolsets import AbstractToolset

from .types import HasLayers, JsonObject, LayerData


def layer_types(
    name: str, registry: Mapping[str, ModuleType]
) -> tuple[
    type[BaseModel],
    type[BaseModel],
    Callable[[str], AbstractCapability[Any] | AbstractToolset[Any]],
]:
    """Reject unknown names or invalid module exports before any component runs."""
    if not name or name not in registry:
        raise UserError(f"Unknown registered layer: {name!r}")
    module = registry[name]
    config = getattr(module, "Config", None)
    state = getattr(module, "State", None)
    for label, model in (("Config", config), ("State", state)):
        if not isinstance(model, type) or not issubclass(model, BaseModel):
            raise UserError(f"Layer {name!r} must export a Pydantic {label}")
    capability = getattr(module, "Capability", None)
    toolset = getattr(module, "Toolset", None)
    if (capability is None) == (toolset is None):
        raise UserError(f"Layer {name!r} must export exactly one Capability or Toolset")
    component = capability if capability is not None else toolset
    expected = AbstractCapability if capability is not None else AbstractToolset
    if not isinstance(component, type) or not issubclass(component, expected):
        raise UserError(f"Layer {name!r} must use native Pydantic AI components")
    return (
        cast(type[BaseModel], config),
        cast(type[BaseModel], state),
        cast(Callable[[str], AbstractCapability[Any] | AbstractToolset[Any]], component),
    )


def dump_object(model: BaseModel) -> JsonObject:
    """Require the Config/State JSON object contract, including RootModel exports."""
    value = model.model_dump(mode="json")
    if not isinstance(value, dict):
        raise UserError("Layer Config and State must encode JSON objects")
    return deepcopy(value)


def load_layers(
    configs: Mapping[str, JsonObject],
    states: Mapping[str, JsonObject],
    registry: Mapping[str, ModuleType],
) -> dict[str, LayerData]:
    """Validate and copy the complete selection; omitted State initializes from {}."""
    unexpected = states.keys() - configs.keys()
    if unexpected:
        raise UserError(f"State for unconfigured layers: {', '.join(sorted(unexpected))}")
    layers: dict[str, LayerData] = {}
    for name, config in configs.items():
        config_type, state_type, _ = layer_types(name, registry)
        layers[name] = {
            "config": dump_object(config_type.model_validate(deepcopy(config))),
            "state": dump_object(state_type.model_validate(deepcopy(states.get(name, {})))),
        }
    return layers


def create_layers[DepsT: HasLayers](
    deps: DepsT, registry: Mapping[str, ModuleType]
) -> dict[str, AbstractCapability[DepsT] | AbstractToolset[DepsT]]:
    """Create fresh native components by name; saved state restores only JSON."""
    return {name: layer_types(name, registry)[2](name) for name in deps.layers}


def snapshot_layers(deps: HasLayers, registry: Mapping[str, ModuleType]) -> dict[str, JsonObject]:
    """Revalidate and copy only State; never persist component instances or Config."""
    return {
        name: dump_object(layer_types(name, registry)[1].model_validate(deepcopy(entry["state"])))
        for name, entry in deps.layers.items()
    }

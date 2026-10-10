"""Allowlisted module construction and explicit JSON state snapshots.

Modules export Config, State and exactly one native Capability or Toolset.
Validators and constructors must be deterministic and perform no I/O; Temporal
reconstructs these components from the same deps in every tool Activity.

Tools (names, collection and schemas) and instructions are static declarations,
stable for fixed Config: never generate them from mutable State through factories.
This preserves model prefix caching. SDK callbacks may render fixed instructions;
dynamic business information belongs in State and tool results.

Optional async module hooks own the longer business scope:
enter_scope(scope_id: UUID, config: Config) -> State initializes a new session;
exit_scope(scope_id: UUID, config: Config, state: State | None) -> None releases it.
The scope ID is the session UUID, unrelated to individual SDK runs or clients.
Exit must be idempotent and clean partial/unacknowledged enter allocations by scope
ID and Config when State is None, including enters that were never called. None
does not mean no resources. Enter need not be generally idempotent: a top-level
RetryableScopeError explicitly promises that this particular call can safely be
repeated. Restarted hosts abandon creating rather than replaying enter.
The host persists identity/Config before enter, and saves complete State only with
ready. Normal exit may mutate State to record confirmed progress; the host saves
valid progress after success, failure or cancellation. Abandoned creation passes
None uniformly, and repeated close may invoke a successful exit again. Late remote
allocations require service-side terminal scope closure or layer reconciliation;
the host lease fences database writes, not external requests. Constructors,
validators and instructions must never allocate scope resources.
"""

from collections.abc import Callable, Mapping
from copy import deepcopy
from inspect import iscoroutinefunction
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
    for hook_name in ("enter_scope", "exit_scope"):
        if hasattr(module, hook_name) and not iscoroutinefunction(getattr(module, hook_name)):
            raise UserError(f"Layer {name!r} {hook_name} must be an async function")
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


def prepare_layer_configs(
    configs: Mapping[str, JsonObject], registry: Mapping[str, ModuleType]
) -> dict[str, BaseModel]:
    """Validate all exports/Config before any scope hook; State may require enter."""
    result = {
        name: layer_types(name, registry)[0].model_validate(deepcopy(config))
        for name, config in configs.items()
    }
    for config in result.values():
        dump_object(config)
    return result


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

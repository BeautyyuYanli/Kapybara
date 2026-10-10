"""Allowlisted static factory assembly and explicit JSON state snapshots.

Factories return typed Layer definitions with native capabilities and explicit
model types. Assembly happens once per process configuration, without execution
deps. Factories and validators are deterministic and perform no I/O; Temporal
reselects and binds the static capabilities from each current deps payload.

Tools (names, collection and schemas) and instructions are static declarations,
stable for fixed Config: never generate them from mutable State through factories.
This preserves model prefix caching. SDK callbacks may render fixed instructions;
dynamic business information belongs in State and tool results.

Optional async Layer hooks own the longer business scope:
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
from typing import Any

from pydantic import BaseModel
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError

from .types import JsonObject, Layer, LayerData, LayerFactory


def make_layers[D](
    registry: Mapping[str, LayerFactory[D]],
    get_layers: Callable[[D], dict[str, LayerData]],
) -> dict[str, Layer[D, Any, Any]]:
    """Assemble all installed factories and check definitions without model/hook I/O.

    Access closes over one ID per invocation and returns the current writable
    entry. The framework never rewrites capability IDs: SDK function Toolsets
    already acquired their IDs during construction.
    """

    def build(layer_id: str, factory: LayerFactory[D]) -> Layer[D, Any, Any]:
        if not layer_id:
            raise UserError("Layer registry names must be nonempty")

        def access(deps: D) -> LayerData:
            return get_layers(deps)[layer_id]

        layer = factory(layer_id, access)
        if not isinstance(layer, Layer):
            raise UserError(f"Layer {layer_id!r} factory must return a Layer")
        for label, model in (("Config", layer.config_type), ("State", layer.state_type)):
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                raise UserError(f"Layer {layer_id!r} must declare a Pydantic {label}")
        if not isinstance(layer.capability, AbstractCapability):
            raise UserError(f"Layer {layer_id!r} must use a native AbstractCapability")
        for label, hook in (("enter_scope", layer.enter_scope), ("exit_scope", layer.exit_scope)):
            if hook is not None and not iscoroutinefunction(hook):
                raise UserError(f"Layer {layer_id!r} {label} must be an async function")
        if layer.capability.id != layer_id:
            raise UserError(
                f"Layer capability id must be {layer_id!r}, got {layer.capability.id!r}"
            )
        return layer

    return {layer_id: build(layer_id, factory) for layer_id, factory in registry.items()}


def dump_object(model: BaseModel) -> JsonObject:
    """Require the Config/State JSON object contract, including RootModel models."""
    value = model.model_dump(mode="json")
    if not isinstance(value, dict):
        raise UserError("Layer Config and State must encode JSON objects")
    return deepcopy(value)


def prepare_layer_configs[D](
    configs: Mapping[str, JsonObject], layers: Mapping[str, Layer[D, Any, Any]]
) -> dict[str, BaseModel]:
    """Validate all names/Config before any scope hook; State may require enter."""
    unknown = configs.keys() - layers.keys()
    if unknown:
        raise UserError(f"Unknown registered layer: {', '.join(sorted(unknown))}")
    result = {
        name: layers[name].config_type.model_validate(deepcopy(config))
        for name, config in configs.items()
    }
    for config in result.values():
        dump_object(config)
    return result


def load_layers[D](
    configs: Mapping[str, JsonObject],
    states: Mapping[str, JsonObject],
    layers: Mapping[str, Layer[D, Any, Any]],
) -> dict[str, LayerData]:
    """Validate and copy existing data; omitted State validates from {}, without hooks."""
    unexpected = states.keys() - configs.keys()
    if unexpected:
        raise UserError(f"State for unconfigured layers: {', '.join(sorted(unexpected))}")
    return {
        name: {
            "config": dump_object(config),
            "state": dump_object(
                layers[name].state_type.model_validate(deepcopy(states.get(name, {})))
            ),
        }
        for name, config in prepare_layer_configs(configs, layers).items()
    }


def snapshot_layers[D](
    deps: D,
    layers: Mapping[str, Layer[D, Any, Any]],
    get_layers: Callable[[D], dict[str, LayerData]],
) -> dict[str, JsonObject]:
    """Revalidate and copy selected State; never persist definitions or Config."""
    selected = get_layers(deps)
    unknown = selected.keys() - layers.keys()
    if unknown:
        raise UserError(f"Unknown registered layer: {', '.join(sorted(unknown))}")
    return {
        name: dump_object(layers[name].state_type.model_validate(deepcopy(entry["state"])))
        for name, entry in selected.items()
    }

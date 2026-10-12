"""Allowlisted static factory assembly and explicit JSON state snapshots.

Factories return typed Layer definitions with native capabilities and explicit
model types. Assembly happens once per process configuration, without execution
deps. Factories and validators are deterministic and perform no I/O; Temporal
reselects and binds the static capabilities from each current deps payload.

Tools (names, collection and schemas) and instructions are static declarations,
stable for fixed Config: never generate them from mutable State through factories.
This preserves model prefix caching. SDK callbacks may render fixed instructions;
dynamic business information belongs in State and tool results.

Optional async Layer hooks describe a longer business scope. The host supplies
its UUID, drives enter/exit and owns persistence and retry scheduling; assembly,
validation and snapshots never invoke those hooks. Exit must be idempotent and
handle partial or unacknowledged initialization with None State. Constructors,
validators and instructions must never allocate business-scope resources.
"""

from collections.abc import Callable, Mapping
from copy import deepcopy
from inspect import iscoroutinefunction
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel
from pydantic_ai.capabilities import AbstractCapability, WrapperCapability
from pydantic_ai.exceptions import UserError

from .types import JsonObject, Layer, LayerData, LayerFactory


class LayerRegistry[D](WrapperCapability[D]):
    """Assemble factories once and install their native per-run selection in an Agent.

    The installed definitions and writable deps access rule are process-local.
    No session data or business resources are retained. Native WrapperCapability
    owns Agent/run rebinding; changing the installation set requires a new registry.
    """

    def __init__(
        self,
        factories: Mapping[str, LayerFactory[D]],
        *,
        get_layers: Callable[[D], dict[str, LayerData]],
    ) -> None:
        self._definitions = MappingProxyType(_make_layers(dict(factories), get_layers))
        self._get_layers = get_layers
        super().__init__(self._build_capability())

    @property
    def definitions(self) -> Mapping[str, Layer[D, Any, Any]]:
        """Read-only installed definitions for host-owned Config/State and scope hooks."""
        return self._definitions

    def prepare_configs(self, configs: Mapping[str, JsonObject]) -> dict[str, BaseModel]:
        """Validate all names and Config before hooks, without constructing State."""
        return _prepare_configs(configs, self.definitions)

    def load(
        self, configs: Mapping[str, JsonObject], states: Mapping[str, JsonObject]
    ) -> dict[str, LayerData]:
        """Validate and copy JSON; omitted State validates from {}, without hooks."""
        return _load(configs, states, self.definitions)

    def snapshot(self, deps: D) -> dict[str, JsonObject]:
        """Validate and copy selected State through this registry's saved access rule."""
        return _snapshot(deps, self.definitions, self._get_layers)

    def _build_capability(self) -> AbstractCapability[D]:
        # Keep optional integrations out of core imports. They override only this
        # assembly step; SDK WrapperCapability still owns all lifecycle delegation.
        from .composition import _layers_capability

        return _layers_capability(self.definitions, get_layers=self._get_layers)


def _make_layers[D](
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


def _prepare_configs[D](
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


def _load[D](
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
        for name, config in _prepare_configs(configs, layers).items()
    }


def _snapshot[D](
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

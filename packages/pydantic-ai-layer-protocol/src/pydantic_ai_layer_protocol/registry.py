"""Allowlisted static factory assembly and explicit JSON state snapshots.

Factories return typed Layer definitions with native capabilities and explicit
model types. Assembly happens once per process configuration, without execution
deps. Factories and validators are deterministic and perform no I/O; native per-run
selection binds the static capabilities to the current writable deps.

Tools (names, collection and schemas) and instructions are static declarations,
stable for fixed Config: never generate them from mutable State through factories.
This preserves model prefix caching. SDK callbacks may render fixed instructions;
dynamic business information belongs in State and tool results.

The host explicitly opens local handles around one or more runs and passes them
in deps. Assembly, JSON validation and snapshots perform no resource I/O.
"""

from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from copy import deepcopy
from inspect import iscoroutinefunction
from types import MappingProxyType
from typing import Any

import anyio
from anyio.lowlevel import checkpoint_if_cancelled
from pydantic import BaseModel
from pydantic_ai.capabilities import AbstractCapability, WrapperCapability
from pydantic_ai.exceptions import UserError

from .types import JsonObject, Layer, LayerAccess, LayerData, LayerFactory


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
        get_resources: Callable[[D], Mapping[str, Any]] | None = None,
    ) -> None:
        self._definitions = MappingProxyType(
            _make_layers(dict(factories), get_layers, get_resources)
        )
        self._get_layers = get_layers
        super().__init__(self._build_capability(get_resources))

    @property
    def definitions(self) -> Mapping[str, Layer[D, Any, Any, Any]]:
        """Read-only installed models, capabilities and resource hooks."""
        return self._definitions

    @asynccontextmanager
    async def open_conversation(
        self, conversation_id: str, configs: Mapping[str, JsonObject]
    ) -> AsyncIterator[dict[str, Any]]:
        """Open configured local handles and close successful enters in reverse order.

        The host passes the yielded mapping to its runs. Each enter owns partial
        initialization until it returns successfully. Enter, the body and exit
        share the caller's task and context. Cancellation from enclosing AnyIO
        scopes is deferred until the exit hooks finish.
        """
        if not isinstance(conversation_id, str) or not conversation_id:
            raise UserError("Layer resources require a nonempty conversation_id")
        prepared = _prepare_configs(configs, self.definitions)
        resources: dict[str, Any] = {}
        stack = AsyncExitStack()
        with anyio.CancelScope() as scope:
            try:
                for name, config in prepared.items():
                    layer = self.definitions[name]
                    if layer.enter_conversation is not None:
                        resource = await layer.enter_conversation(conversation_id, config)
                        resources[name] = resource
                        if layer.exit_conversation is not None:
                            stack.push_async_callback(layer.exit_conversation, resource)
                yield resources
            finally:
                # Shield outside any cancel scopes opened by the enter hooks.
                scope.shield = True
                await stack.aclose()
        # Surface cancellation that arrived while cleanup was shielded.
        await checkpoint_if_cancelled()

    def load(
        self, configs: Mapping[str, JsonObject], states: Mapping[str, JsonObject]
    ) -> dict[str, LayerData]:
        """Validate and copy JSON; omitted State validates from {}, without hooks."""
        return _load(configs, states, self.definitions)

    def snapshot(self, deps: D) -> dict[str, JsonObject]:
        """Validate and copy selected State through this registry's saved access rule."""
        return _snapshot(deps, self.definitions, self._get_layers)

    def _build_capability(
        self, get_resources: Callable[[D], Mapping[str, Any]] | None
    ) -> AbstractCapability[D]:
        from .composition import _layers_capability

        return _layers_capability(
            self.definitions, get_layers=self._get_layers, get_resources=get_resources
        )


def _make_layers[D](
    registry: Mapping[str, LayerFactory[D]],
    get_layers: Callable[[D], dict[str, LayerData]],
    get_resources: Callable[[D], Mapping[str, Any]] | None,
) -> dict[str, Layer[D, Any, Any, Any]]:
    """Assemble all installed factories and check definitions without model/hook I/O.

    Access closes over one ID per invocation and returns the current writable
    entry. The framework never rewrites capability IDs: SDK function Toolsets
    already acquired their IDs during construction.
    """

    def build(layer_id: str, factory: LayerFactory[D]) -> Layer[D, Any, Any, Any]:
        if not layer_id:
            raise UserError("Layer registry names must be nonempty")

        def data(deps: D) -> LayerData:
            return get_layers(deps)[layer_id]

        def resource(deps: D) -> Any:
            resources = get_resources(deps) if get_resources is not None else {}
            if layer_id not in resources:
                raise UserError(f"Layer {layer_id!r} has no open resource")
            return resources[layer_id]

        layer = factory(layer_id, LayerAccess(data, resource))
        if not isinstance(layer, Layer):
            raise UserError(f"Layer {layer_id!r} factory must return a Layer")
        for label, model in (("Config", layer.config_type), ("State", layer.state_type)):
            if not isinstance(model, type) or not issubclass(model, BaseModel):
                raise UserError(f"Layer {layer_id!r} must declare a Pydantic {label}")
        if not isinstance(layer.capability, AbstractCapability):
            raise UserError(f"Layer {layer_id!r} must use a native AbstractCapability")
        for label, hook in (
            ("enter_conversation", layer.enter_conversation),
            ("exit_conversation", layer.exit_conversation),
        ):
            if hook is not None and not iscoroutinefunction(hook):
                raise UserError(f"Layer {layer_id!r} {label} must be an async function")
        if layer.enter_conversation is None and layer.exit_conversation is not None:
            raise UserError(f"Layer {layer_id!r} exit_conversation requires enter_conversation")
        if layer.enter_conversation is not None and get_resources is None:
            raise UserError(f"Layer {layer_id!r} enter_conversation requires get_resources")
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
    configs: Mapping[str, JsonObject], layers: Mapping[str, Layer[D, Any, Any, Any]]
) -> dict[str, BaseModel]:
    """Validate and copy all Config before constructing State or opening handles."""
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
    layers: Mapping[str, Layer[D, Any, Any, Any]],
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
    layers: Mapping[str, Layer[D, Any, Any, Any]],
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

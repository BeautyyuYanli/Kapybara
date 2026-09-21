"""Process-local trusted definitions; one current implementation per provider/name.

Migrations and custom validators are pure builtin code, run outside transactions.
Untrusted classes/callables must never be registered here; a future isolated
runtime needs trusted JSON proxies instead.
"""

import json
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from pydantic import BaseModel, JsonValue, TypeAdapter

from .contracts import AgentPlugin, PluginData

_json = TypeAdapter(JsonValue)


def check_name(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_]+", value) is None:
        raise ValueError("Plugin and tool names require nonempty ASCII letters/digits/underscores")


def json_copy(value: Any) -> JsonValue:
    """Sever references and reject non-JSON values including nonfinite numbers."""
    parsed = _json.validate_python(value, strict=True)
    return json.loads(json.dumps(parsed, allow_nan=False))


def encode_model(value: BaseModel) -> JsonValue:
    """Use Pydantic's round-trip form, including aliases and Json[T] wrappers."""
    return json_copy(value.model_dump(mode="json", by_alias=True, round_trip=True))


def validate_model(model: type[BaseModel], value: JsonValue) -> tuple[BaseModel, JsonValue]:
    parsed = model.model_validate(deepcopy(value))
    encoded = encode_model(parsed)
    # Custom serializers may produce valid JSON that the current model cannot
    # read. Reject it before any transaction persists an unreadable binding.
    model.model_validate(deepcopy(encoded))
    return parsed, encoded


@dataclass(frozen=True)
class PluginDefinition:
    """One current trusted implementation and shared config/state data format.

    Register once per provider/name with a no-argument AgentPlugin class and
    Pydantic config/state types. Validators and serializers are pure data code;
    their JSON output must be readable by the registered type before persistence.
    Each migrations[n] converts raw PluginData from n to n+1 without I/O, context
    or resource operations. Keep the steps needed by stored bindings and retain
    resource cleanup references; old implementation classes are not required.
    """

    plugin_provider: str
    plugin_name: str
    data_version: int
    config_type: type[BaseModel]
    state_type: type[BaseModel]
    plugin_type: type[AgentPlugin[Any, Any]]
    migrations: Mapping[int, Callable[[PluginData], PluginData]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_name(self.plugin_provider)
        check_name(self.plugin_name)
        if type(self.data_version) is not int or self.data_version < 1:
            raise ValueError("data_version must be a positive integer")
        if any(type(key) is not int or not 0 < key < self.data_version for key in self.migrations):
            raise ValueError("Migration keys must precede the target data_version")
        if not all(callable(function) for function in self.migrations.values()):
            raise ValueError("Migrations must be callable")
        for model in (self.config_type, self.state_type):
            if not issubclass(model, BaseModel):
                raise ValueError("Plugin data types must be Pydantic models")
        if not issubclass(self.plugin_type, AgentPlugin):
            raise ValueError("plugin_type must implement AgentPlugin")
        object.__setattr__(self, "migrations", MappingProxyType(dict(self.migrations)))

    def load(self, version: int, data: PluginData) -> tuple[BaseModel, PluginData]:
        """Migrate contiguous JSON steps then validate current types, without I/O."""
        if version > self.data_version:
            raise ValueError("Persisted plugin data is newer than the registered definition")
        data = PluginData(json_copy(data.config), json_copy(data.state))
        for source in range(version, self.data_version):
            try:
                migrate = self.migrations[source]
            except KeyError:
                raise ValueError(f"Missing plugin data migration from {source}") from None
            data = migrate(data)
            if not isinstance(data, PluginData):
                raise ValueError("Migration must return PluginData")
            data = PluginData(json_copy(data.config), json_copy(data.state))
        config, config_json = validate_model(self.config_type, data.config)
        state = None if data.state is None else validate_model(self.state_type, data.state)[1]
        return config, PluginData(config_json, state)


class PluginRegistry:
    def __init__(self) -> None:
        self._definitions: dict[tuple[str, str], PluginDefinition] = {}

    def register(self, definition: PluginDefinition) -> None:
        key = (definition.plugin_provider, definition.plugin_name)
        if key in self._definitions:
            raise ValueError(f"Duplicate plugin {key}")
        self._definitions[key] = definition

    def get(self, provider: str, name: str) -> PluginDefinition:
        try:
            return self._definitions[provider, name]
        except KeyError:
            raise LookupError(f"Plugin {provider}.{name} is not registered") from None

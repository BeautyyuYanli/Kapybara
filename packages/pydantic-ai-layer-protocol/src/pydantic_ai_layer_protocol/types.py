"""Typed static layer definitions and injected access to JSON data and local handles.

Every layer reads its own ``config`` and ``state`` and explicitly dumps changes
back to ``state``. Resources and SDK component instances never enter this JSON.
Data access returns the actual writable entry in current deps. D is unbounded;
layers treat deps as opaque and use injected access for both data and handles.
Layer definitions retain neither. The SDK owns per-run capability binding.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypedDict

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError

type JsonObject = dict[str, JsonValue]


class LayerData(TypedDict):
    config: JsonObject
    state: JsonObject


@dataclass(frozen=True, kw_only=True)
class Layer[D, C: BaseModel, S: BaseModel, R]:
    """Installed models, capability and optional local resource hooks.

    Capability/Toolset IDs stay fixed after construction. Stateful SDK components
    use native ``for_run`` isolation. Enter builds a local handle from Config and
    releases anything it constructed if it fails before returning. Exit receives
    only the returned handle and closes it without changing State or remote resources.
    """

    config_type: type[C]
    state_type: type[S]
    capability: AbstractCapability[D]
    enter_conversation: Callable[[str, C], Awaitable[R]] | None = None
    exit_conversation: Callable[[R], Awaitable[None]] | None = None

    @property
    def id(self) -> str:
        value = self.capability.id
        if not value:
            raise UserError("Layer capability must have a nonempty id")
        return value


@dataclass(frozen=True)
class LayerAccess[D]:
    """Read this layer's writable JSON entry and its open runtime handle."""

    _data: Callable[[D], LayerData]
    _resource: Callable[[D], Any]

    def data(self, deps: D) -> LayerData:
        return self._data(deps)

    def resource(self, deps: D) -> Any:
        """Raise UserError if the current deps have no handle for this layer."""
        return self._resource(deps)


type LayerFactory[D] = Callable[[str, LayerAccess[D]], Layer[D, Any, Any, Any]]


class LayerDeps(BaseModel):
    """Standalone SDK deps with serialized layer data and transient handles."""

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)
    layers: dict[str, LayerData] = Field(default_factory=dict)
    resources: dict[str, Any] = Field(default_factory=dict, exclude=True)

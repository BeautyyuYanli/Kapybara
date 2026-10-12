"""Typed static layer definitions and injected access to writable JSON dependencies.

Every layer reads its own ``config`` and ``state`` and explicitly dumps changes
back to ``state``. Resources and SDK component instances never enter this JSON.
Access functions return the actual entry in current deps, never a reconstructed
copy. D is unbounded; layers treat deps as opaque and obtain all business data
through injected access and their Config/State models. Layer definitions contain
no session data; the SDK owns per-run binding.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import UserError

type JsonObject = dict[str, JsonValue]


class LayerData(TypedDict):
    config: JsonObject
    state: JsonObject


@dataclass(frozen=True, kw_only=True)
class Layer[D, C: BaseModel, S: BaseModel]:
    """Installed models, capability and independent session-scope hooks.

    Capability/Toolset IDs stay fixed after construction. Stateful SDK components
    use native ``for_run`` isolation. Exit must tolerate absent persisted State,
    partial allocation and repeated cleanup using the host's scope UUID and Config.
    """

    config_type: type[C]
    state_type: type[S]
    capability: AbstractCapability[D]
    enter_scope: Callable[[UUID, C], Awaitable[S]] | None = None
    exit_scope: Callable[[UUID, C, S | None], Awaitable[None]] | None = None

    @property
    def id(self) -> str:
        value = self.capability.id
        if not value:
            raise UserError("Layer capability must have a nonempty id")
        return value


type LayerAccess[D] = Callable[[D], LayerData]
type LayerFactory[D] = Callable[[str, LayerAccess[D]], Layer[D, Any, Any]]


class LayerDeps(BaseModel):
    """Minimal serializable deps for standalone SDK consumers and examples."""

    model_config = ConfigDict(extra="forbid")
    layers: dict[str, LayerData] = Field(default_factory=dict)

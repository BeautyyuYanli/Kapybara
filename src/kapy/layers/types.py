"""JSON dependencies for native layers; validated models are disposable copies.

Every layer reads its own ``config`` and ``state`` and explicitly dumps changes
back to ``state``. Resources and SDK component instances never enter this JSON.
"""

from typing import Protocol, TypedDict

from pydantic import BaseModel, ConfigDict, Field, JsonValue

type JsonObject = dict[str, JsonValue]


class LayerData(TypedDict):
    config: JsonObject
    state: JsonObject


class HasLayers(Protocol):
    layers: dict[str, LayerData]


class LayerDeps(BaseModel):
    """Minimal serializable deps for standalone SDK consumers and examples."""

    model_config = ConfigDict(extra="forbid")
    layers: dict[str, LayerData] = Field(default_factory=dict)

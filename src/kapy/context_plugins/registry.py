"""Names map to ordinary constructors; closures can bind plugin dependencies."""

from collections.abc import Callable
from copy import deepcopy

from pydantic import BaseModel, ConfigDict, Field

from kapy.agent_runner.context import ContextPlugin, JsonObject

from .summary import SummaryPlugin


class ContextPluginSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="kapy/summary", min_length=1)
    config: JsonObject = Field(default_factory=dict)


type ContextPluginFactory = Callable[[JsonObject], ContextPlugin]


class ContextPluginRegistry:
    def __init__(self, factories: dict[str, ContextPluginFactory]) -> None:
        self._factories = dict(factories)

    def create(self, spec: ContextPluginSpec) -> ContextPlugin:
        try:
            factory = self._factories[spec.name]
        except KeyError:
            raise ValueError(f"Unknown context plugin: {spec.name}") from None
        plugin = factory(deepcopy(spec.config))
        if not isinstance(plugin.key, str) or not plugin.key.strip():
            raise ValueError("Context plugin key must be nonempty")
        return plugin


def create_default_registry() -> ContextPluginRegistry:
    def summary(config: JsonObject) -> ContextPlugin:
        if config:
            raise ValueError("kapy/summary does not accept configuration")
        return SummaryPlugin()

    return ContextPluginRegistry({"kapy/summary": summary})

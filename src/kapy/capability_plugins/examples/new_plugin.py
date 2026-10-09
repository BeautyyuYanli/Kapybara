"""Minimal plugin foundation: a configuration model and a pure capability factory."""

from pydantic import BaseModel, ConfigDict
from pydantic_ai.capabilities import AbstractCapability, Capability

from kapy.capability_plugins import PluginDeps


class MyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    instructions: str


def build_my_plugin(config: MyConfig) -> AbstractCapability[PluginDeps]:
    """Register with registry.register('my_plugin', config_type=MyConfig, factory=...)."""
    return Capability(id="my_plugin", instructions=config.instructions)

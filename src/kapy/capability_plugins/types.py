"""Serializable, complete plugin selections passed through agent dependencies."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, JsonValue

PluginName = Annotated[str, Field(min_length=1)]


class PluginSpec(BaseModel):
    """One enabled plugin and its full configuration, including JSON null.

    Nested configuration is read-only input by convention: ``frozen`` only
    prevents field assignment. The registry copies it before plugin validation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plugin_name: PluginName
    config: JsonValue = Field(default_factory=dict)


class PluginDeps(BaseModel):
    """Run input; an empty selection disables all plugins.

    Consumers may subclass this DTO to add their own serializable dependencies
    and use that subclass as the Agent's ``deps_type``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plugins: tuple[PluginSpec, ...] = ()

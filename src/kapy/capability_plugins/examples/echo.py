"""A stateless tool configured independently for each run."""

from pydantic import BaseModel, ConfigDict
from pydantic_ai.capabilities import AbstractCapability, Capability

from kapy.capability_plugins import PluginDeps


class EchoConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    prefix: str = ""
    uppercase: bool = False


def build_echo(config: EchoConfig) -> AbstractCapability[PluginDeps]:
    def example_echo(text: str) -> str:
        """Return text formatted with this run's configuration."""
        value = text.upper() if config.uppercase else text
        return f"{config.prefix}{value}"

    return Capability(
        id="echo",
        instructions="需要回显文本时调用 example_echo 工具。",
        tools=[example_echo],
    )

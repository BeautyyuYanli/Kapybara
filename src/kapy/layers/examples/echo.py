"""A native capability reading config and explicitly saving JSON state."""

from pydantic import BaseModel, ConfigDict
from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability as NativeCapability

from kapy.layers.types import LayerDeps


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = ""
    uppercase: bool = False


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls: int = 0


class Capability(NativeCapability[LayerDeps]):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            id=name,
            instructions="需要回显文本时调用 example_echo 工具。",
            tools=[Tool(self.echo, name="example_echo")],
        )

    def echo(self, ctx: RunContext[LayerDeps], text: str) -> str:
        """Return text formatted with this run's config and retain a call count."""
        entry = ctx.deps.layers[self.name]
        config, state = Config.model_validate(entry["config"]), State.model_validate(entry["state"])
        value = text.upper() if config.uppercase else text
        state.calls += 1
        entry["state"] = state.model_dump(mode="json")
        return f"{config.prefix}{value}"

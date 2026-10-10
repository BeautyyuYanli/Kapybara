"""A native capability reading config and explicitly saving JSON state."""

from pydantic import BaseModel, ConfigDict
from pydantic_ai import RunContext, Tool
from pydantic_ai.capabilities import Capability

from kapy.layers.modules import dump_object
from kapy.layers.types import Layer, LayerAccess


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prefix: str = ""
    uppercase: bool = False


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls: int = 0


def make[DepsT](layer_id: str, access: LayerAccess[DepsT]) -> Layer[DepsT, Config, State]:
    """Build once; injected access reads the actual entry in each current deps."""

    def echo(ctx: RunContext[DepsT], text: str) -> str:
        """Return text formatted with this run's config and retain a call count."""
        entry = access(ctx.deps)
        config, state = Config.model_validate(entry["config"]), State.model_validate(entry["state"])
        value = text.upper() if config.uppercase else text
        state.calls += 1
        access(ctx.deps)["state"] = dump_object(state)
        return f"{config.prefix}{value}"

    return Layer(
        config_type=Config,
        state_type=State,
        capability=Capability(
            id=layer_id,
            instructions="需要回显文本时调用 example_echo 工具。",
            tools=[Tool(echo, name="example_echo")],
        ),
    )

"""Minimal typed factory; model types may also be defined inside make."""

from pydantic import BaseModel, ConfigDict
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability

from kapy.layers.types import Layer, LayerAccess


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")


def make[DepsT](layer_id: str, access: LayerAccess[DepsT]) -> Layer[DepsT, Config, State]:
    def instructions(ctx: RunContext[DepsT]) -> str:
        return Config.model_validate(access(ctx.deps)["config"]).instructions

    return Layer(
        config_type=Config,
        state_type=State,
        capability=Capability(id=layer_id, instructions=instructions),
    )

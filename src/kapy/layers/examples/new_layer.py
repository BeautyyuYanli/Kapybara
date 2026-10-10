"""Minimal fixed module exports; register this module by an installed name."""

from pydantic import BaseModel, ConfigDict
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability

from kapy.layers.types import LayerDeps


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: str


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Capability(AbstractCapability[LayerDeps]):
    def __init__(self, name: str) -> None:
        self.id = self.name = name

    def get_instructions(self):
        return self.instructions

    def instructions(self, ctx: RunContext[LayerDeps]) -> str:
        return Config.model_validate(ctx.deps.layers[self.name]["config"]).instructions

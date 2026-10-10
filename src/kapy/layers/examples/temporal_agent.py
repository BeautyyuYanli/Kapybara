"""Register static leaf Activity entries shared by the example Worker and Workflow."""

from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.models.test import TestModel

from kapy.layers import LayerData, LayerDeps, layers_capability, make_layers
from kapy.layers.examples import echo

registry = {"echo": echo.make}


def get_layers(deps: LayerDeps) -> dict[str, LayerData]:
    return deps.layers


layers = make_layers(registry, get_layers)
agent = Agent(
    TestModel(),
    name="layers_example",
    deps_type=LayerDeps,
    capabilities=[layers_capability(layers, get_layers=get_layers), TemporalDurability()],
)

"""Register static leaf Activity entries shared by the example Worker and Workflow."""

from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.models.test import TestModel

from pydantic_ai_layer_protocol import LayerData, LayerDeps
from pydantic_ai_layer_protocol.examples import echo
from pydantic_ai_layer_protocol.temporal import TemporalLayerRegistry

registry = {"echo": echo.make}


def get_layers(deps: LayerDeps) -> dict[str, LayerData]:
    return deps.layers


layers = TemporalLayerRegistry(registry, get_layers=get_layers)
agent = Agent(
    TestModel(),
    name="layers_example",
    deps_type=LayerDeps,
    capabilities=[layers, TemporalDurability()],
)

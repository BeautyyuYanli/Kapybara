"""Static registration shared by the standalone example Worker and Workflow."""

from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.models.test import TestModel

from kapy.layers import LayerDeps, layers_capability
from kapy.layers.examples import echo

registry = {"echo": echo}
agent = Agent(
    TestModel(),
    name="layers_example",
    deps_type=LayerDeps,
    capabilities=[layers_capability(registry), TemporalDurability()],
)

"""Shared static registration for the example Worker and Workflow."""

from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.models.test import TestModel

from kapy.capability_plugins import PluginDeps, PluginRegistry
from kapy.capability_plugins.examples.echo import EchoConfig, build_echo

registry = PluginRegistry[PluginDeps]()
registry.register("echo", config_type=EchoConfig, factory=build_echo)
agent = Agent(
    TestModel(),
    name="capability_plugins_example",
    deps_type=PluginDeps,
    capabilities=[registry.capability(id="plugins"), TemporalDurability()],
)

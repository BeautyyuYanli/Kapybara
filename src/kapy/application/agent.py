"""One application Agent factory for HTTP and Telegram, fresh for each runner lease.

Only this composition layer registers builtin definitions. Interfaces never
select different plugins for the same session. Plugin contexts enclose all SDK
runs and compaction, and are released before the runner relinquishes ownership.
"""

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability

from kapy.agent_plugins import AgentPluginService, PluginRegistry
from kapy.agent_plugins.builtin.shell import SHELL_PLUGIN
from kapy.agent_plugins.capability import PluginCapabilityAdapter
from kapy.agent_runner import RunnerExecution


def create_registry() -> PluginRegistry:
    """Register builtin definitions; sessions opt in through CreateSession.plugins."""
    registry = PluginRegistry()
    registry.register(SHELL_PLUGIN)
    return registry


def create_agent(*, capabilities: Sequence[AbstractCapability[Any]] = ()) -> Agent[None, str]:
    return Agent(instructions="Be concise and precise.", output_type=str, capabilities=capabilities)


def create_execution_factory(plugins: AgentPluginService):
    @asynccontextmanager
    async def factory(session_id: UUID) -> AsyncIterator[RunnerExecution[str]]:
        async with plugins.open_execution(session_id) as bindings:
            names: set[str] = set()
            capabilities = [PluginCapabilityAdapter.build(*binding, names) for binding in bindings]
            yield RunnerExecution(create_agent(capabilities=capabilities))

    return factory

"""One application execution factory for HTTP and Telegram, fresh per runner lease.

Only this composition layer registers builtin definitions. Interfaces never
select different plugins for the same session. Model/plugin contexts enclose all
SDK runs and paging, and close before lease release. Business capabilities are
collected here; the runner installs them when opening each main or auxiliary run.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings

from kapy.agent_plugins import AgentPluginService, PluginRegistry
from kapy.agent_plugins.builtin.shell import SHELL_PLUGIN
from kapy.agent_plugins.capability import PluginCapabilityAdapter
from kapy.agent_runner import RunnerExecution
from kapy.context_plugins import ContextPluginRegistry
from kapy.control.models.runtime import build_model, build_provider
from kapy.control.sessions.service import (
    SessionExecutionConfig,
    SessionExecutionFactory,
    SessionReadyCapability,
    SessionService,
)


def create_registry() -> PluginRegistry:
    """Register builtin definitions; sessions opt in through CreateSession.plugins."""
    registry = PluginRegistry()
    registry.register(SHELL_PLUGIN)
    return registry


def create_agent(
    *, model: Model | None = None, model_settings: ModelSettings | None = None
) -> Agent[None, str]:
    """Create a base Agent; business capabilities belong to RunnerExecution."""
    return Agent(
        model=model,
        model_settings=model_settings,
        instructions="Be concise and precise.",
        output_type=str,
    )


def create_execution_factory(plugins: AgentPluginService) -> SessionExecutionFactory:
    """Compose each leased execution from the service's snapshot and context registry.

    The service supplies business lifecycle checks; this factory owns model/plugin
    contexts, the base Agent, capabilities and paging configuration. It neither
    installs capabilities nor rereads mutable session/model configuration.
    """

    @asynccontextmanager
    async def factory(
        service: SessionService,
        config: SessionExecutionConfig,
        context_plugin_registry: ContextPluginRegistry,
    ) -> AsyncIterator[RunnerExecution[str]]:
        session = config.session
        await service.require_ready(session.id)
        async with (
            build_provider(config.provider_class, config.provider) as provider,
            build_model(
                config.model_class,
                config.model.model_name,
                provider,
                profile={"context_window": config.model.context_window},
            ) as model,
            plugins.open_execution(session.id) as bindings,
        ):
            names: set[str] = set()
            capabilities = (
                SessionReadyCapability(service, session.id),
                *(
                    capability
                    for binding in bindings
                    for capability in PluginCapabilityAdapter.build(*binding, names)
                ),
            )
            context_plugin = context_plugin_registry.create(session.context_plugin)
            agent = create_agent(model=model, model_settings=config.model_settings)
            await service.require_ready(session.id)
            yield RunnerExecution(
                agent=agent,
                capabilities=capabilities,
                context_plugin=context_plugin,
                compaction_threshold_tokens=config.compaction_threshold_tokens,
                compaction_replay_turns=session.compaction_replay_turns,
            )

    return factory

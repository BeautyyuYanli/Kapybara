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
from kapy.agent_plugins.builtin.response_rewrite import RESPONSE_REWRITE_PLUGIN
from kapy.agent_plugins.builtin.shell import SHELL_PLUGIN
from kapy.agent_plugins.capability import PluginCapabilityAdapter
from kapy.agent_runner import RunnerExecution
from kapy.context_plugins import ContextPluginRegistry
from kapy.control.models.runtime import build_model, build_provider
from kapy.control.sessions.service import (
    SessionExecutionConfig,
    SessionExecutionFactory,
    SessionService,
)
from kapy.session_lease import SessionLease


def create_registry() -> PluginRegistry:
    """Register builtin definitions; sessions opt in through CreateSession.plugins."""
    registry = PluginRegistry()
    registry.register(SHELL_PLUGIN)
    registry.register(RESPONSE_REWRITE_PLUGIN)
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

    This factory owns admission on every entry, including lease reacquisition:
    lock_owned and require_ready share a short transaction before resources open.
    Ownership alone does not imply READY; the snapshot's status may be stale.
    It then owns model/plugin contexts, the base Agent, capabilities and paging
    configuration, without rereading model settings or installing capabilities.
    """

    @asynccontextmanager
    async def factory(
        service: SessionService,
        config: SessionExecutionConfig,
        context_plugin_registry: ContextPluginRegistry,
        lease: SessionLease,
    ) -> AsyncIterator[RunnerExecution[str]]:
        session = config.session
        async with plugins.session_factory.begin() as db:
            await lease.lock_owned(db)
            await service.require_ready(session.id, db=db)
        async with (
            build_provider(config.provider_class, config.provider) as provider,
            build_model(
                config.model_class,
                config.model.model_name,
                provider,
                profile={"context_window": config.model.context_window},
            ) as model,
            plugins.open_execution(session.id, lease=lease) as bindings,
        ):
            names: set[str] = set()
            capabilities = tuple(
                capability
                for binding in bindings
                for capability in PluginCapabilityAdapter.build(*binding, names)
            )
            context_plugin = context_plugin_registry.create(session.context_plugin)
            agent = create_agent(model=model, model_settings=config.model_settings)
            yield RunnerExecution(
                agent=agent,
                capabilities=capabilities,
                context_plugin=context_plugin,
                compaction_threshold_tokens=config.compaction_threshold_tokens,
                compaction_replay_turns=session.compaction_replay_turns,
            )

    return factory

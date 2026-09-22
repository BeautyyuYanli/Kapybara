"""Shared session composition for every interface process.

The registry selects per-session context plugins. HTTP and Telegram pass stored
configuration through the same service; neither owns paging behavior.
"""

from kapy.agent_output import AgentOutputService
from kapy.agent_plugins import AgentPluginService, PluginRegistry
from kapy.context_plugins import ContextPluginRegistry, create_default_registry
from kapy.control.sessions import SessionService

from .agent import create_execution_factory, create_registry
from .resources import Resources
from .settings import CommonSettings


def create_session_service(
    resources: Resources,
    settings: CommonSettings,
    *,
    context_plugin_registry: ContextPluginRegistry | None = None,
    plugin_registry: PluginRegistry | None = None,
) -> SessionService:
    plugins = AgentPluginService(
        resources.core_session_factory, plugin_registry or create_registry()
    )
    return SessionService(
        resources.core_session_factory,
        output_service=AgentOutputService(
            resources.valkey, channel_prefix=settings.valkey_namespace + ":agent-output"
        ),
        heartbeat_interval=settings.heartbeat_interval,
        heartbeat_timeout=settings.heartbeat_timeout,
        takeover_grace_period=settings.takeover_grace_period,
        context_plugin_registry=context_plugin_registry or create_default_registry(),
        plugin_service=plugins,
        execution_factory=create_execution_factory(plugins),
    )

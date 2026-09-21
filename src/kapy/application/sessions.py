"""Shared session composition for every interface process.

The default policy is summary/v1. Applications may inject one common factory
here; HTTP and Telegram do not select context strategies in their controllers.
"""

from kapy.agent_output import AgentOutputService
from kapy.agent_plugins import AgentPluginService, PluginRegistry
from kapy.control.sessions import SessionService
from kapy.control.sessions.service import ContextPolicyFactory

from .agent import create_execution_factory, create_registry
from .resources import Resources
from .settings import CommonSettings


def create_session_service(
    resources: Resources,
    settings: CommonSettings,
    *,
    context_policy_factory: ContextPolicyFactory | None = None,
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
        context_policy_factory=context_policy_factory,
        plugin_service=plugins,
        execution_factory=create_execution_factory(plugins),
    )

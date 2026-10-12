"""Shared session composition; durable Worker owns model and tool resources."""

from kapy.agent_output import AgentOutputService
from kapy.control.sessions import SessionService

from .resources import Resources
from .settings import CommonSettings


def create_session_service(resources: Resources, settings: CommonSettings) -> SessionService:
    return SessionService(
        resources.core_session_factory,
        output_service=AgentOutputService(
            resources.valkey, channel_prefix=settings.valkey_namespace + ":agent-output"
        ),
        temporal_client=resources.temporal_client,
        temporal_task_queue=settings.temporal_task_queue,
        heartbeat_interval=settings.heartbeat_interval,
        heartbeat_timeout=settings.heartbeat_timeout,
        takeover_grace_period=settings.takeover_grace_period,
    )

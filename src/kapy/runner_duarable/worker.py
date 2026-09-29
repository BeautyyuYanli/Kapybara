"""Independent Temporal Worker; SIGTERM drains it before the process exits.

The Worker executes both Workflow and Activity tasks on the configured queue.
It shares the application resource factory; resources outlive all Worker tasks.
Database and Valkey clients belong to Activities, never Workflow state.
"""

import asyncio
import logging
import os
import signal
from datetime import timedelta

from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.worker import Worker

from kapy.agent_output import AgentOutputService
from kapy.application.resources import open_resources
from kapy.application.settings import CommonSettings

from .activities import RunnerActivities
from .agent import agent
from .context import bind_output_service
from .workflow import RunnerWorkflow


async def serve(settings: CommonSettings) -> None:
    async with open_resources(settings) as resources:
        output_service = AgentOutputService(
            resources.valkey, channel_prefix=settings.valkey_namespace + ":agent-output"
        )
        activities = RunnerActivities(resources.core_session_factory, output_service)
        with bind_output_service(output_service):
            async with Worker(
                resources.temporal_client,
                task_queue=settings.temporal_task_queue,
                workflows=[RunnerWorkflow],
                activities=[activities.record_messages, activities.save_runner_state],
                plugins=[AgentPlugin(agent)],
                graceful_shutdown_timeout=timedelta(seconds=15),
            ):
                await asyncio.Future()


async def _serve_with_signals(settings: CommonSettings) -> None:
    loop, task = asyncio.get_running_loop(), asyncio.current_task()
    assert task is not None
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        await serve(settings)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)


def main() -> None:
    settings = CommonSettings.model_validate(dict(os.environ))
    logging.basicConfig(level=settings.log_level)
    try:
        asyncio.run(_serve_with_signals(settings))
    except KeyboardInterrupt, asyncio.CancelledError:
        pass


if __name__ == "__main__":
    main()

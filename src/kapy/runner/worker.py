"""Independent Temporal Worker; SIGTERM drains it before the process exits.

The Worker executes create/run/close standalone Activities on the configured queue.
It shares the application resource factory; resources outlive all Worker tasks.
Database and Valkey clients outlive all Activity calls.
"""

import asyncio
import logging
import os
import signal
from datetime import timedelta

from temporalio.worker import Worker

from kapy.agent_output import AgentOutputService
from kapy.application.resources import open_resources
from kapy.application.settings import CommonSettings

from .activities import RunnerActivity


async def serve(settings: CommonSettings) -> None:
    async with open_resources(settings) as resources:
        output_service = AgentOutputService(
            resources.valkey, channel_prefix=settings.valkey_namespace + ":agent-output"
        )
        runner = RunnerActivity(
            resources.core_session_factory, output_service, resources.temporal_client
        )
        async with Worker(
            resources.temporal_client,
            task_queue=settings.temporal_task_queue,
            activities=[runner.create, runner.run, runner.close],
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

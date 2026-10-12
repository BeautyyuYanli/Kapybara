"""A real separate owner process; it needs neither PostgreSQL nor the output service."""

import asyncio
import os
import signal
import sys
from datetime import timedelta

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from kapy.runner_duarable import hold_mcp_gateway


async def serve() -> None:
    task = asyncio.current_task()
    assert task is not None
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    client = await Client.connect(
        os.environ["KAPY_TEMPORAL_ADDRESS"],
        namespace=os.environ.get("KAPY_TEMPORAL_NAMESPACE", "default"),
        plugins=[PydanticAIPlugin()],
    )
    async with Worker(
        client,
        task_queue=sys.argv[1] + "-mcp",
        activities=[hold_mcp_gateway],
        identity="external-mcp-owner",
        graceful_shutdown_timeout=timedelta(seconds=0),
    ):
        print("ready", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(serve())
    except asyncio.CancelledError:
        pass

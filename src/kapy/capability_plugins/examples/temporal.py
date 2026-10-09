"""Own the example Client/Worker and execute a plugin-enabled Workflow.

Requires a Temporal service at localhost:7233; execute with
``python -m kapy.capability_plugins.examples.temporal``.
"""

import asyncio
from uuid import uuid4

from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from kapy.capability_plugins import PluginDeps, PluginSpec
from kapy.capability_plugins.examples.temporal_agent import agent
from kapy.capability_plugins.examples.temporal_workflow import PluginExampleWorkflow


async def main() -> None:
    client = await Client.connect("localhost:7233", plugins=[PydanticAIPlugin()])
    task_queue = "capability-plugins-example"
    deps = PluginDeps(plugins=(PluginSpec(plugin_name="echo", config={"prefix": "Temporal:"}),))
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[PluginExampleWorkflow],
        plugins=[AgentPlugin(agent)],
    ):
        result = await client.execute_workflow(
            PluginExampleWorkflow.run,
            args=["回显文本", deps],
            id=f"capability-plugins-example-{uuid4()}",
            task_queue=task_queue,
        )
        print(result)


if __name__ == "__main__":
    asyncio.run(main())

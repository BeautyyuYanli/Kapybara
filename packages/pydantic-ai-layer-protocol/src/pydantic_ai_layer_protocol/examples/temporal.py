"""Own the example Client/Worker and execute a layer-enabled Workflow.

Requires a Temporal service at localhost:7233; execute with
``python -m pydantic_ai_layer_protocol.examples.temporal``.
"""

import asyncio
from uuid import uuid4

from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from temporalio.client import Client
from temporalio.worker import Worker

from pydantic_ai_layer_protocol import LayerDeps
from pydantic_ai_layer_protocol.examples.temporal_agent import agent, layers
from pydantic_ai_layer_protocol.examples.temporal_workflow import LayerExampleWorkflow


async def main() -> None:
    client = await Client.connect("localhost:7233", plugins=[PydanticAIPlugin()])
    task_queue = "layers-example"
    deps = LayerDeps(layers=layers.load({"echo": {"prefix": "Temporal:"}}, {}))
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[LayerExampleWorkflow],
        plugins=[AgentPlugin(agent)],
    ):
        result = await client.execute_workflow(
            LayerExampleWorkflow.run,
            args=["回显文本", deps],
            id=f"layers-example-{uuid4()}",
            task_queue=task_queue,
        )
        print(result)


if __name__ == "__main__":
    asyncio.run(main())

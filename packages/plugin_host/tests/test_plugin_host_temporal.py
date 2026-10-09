"""Real Temporal Worker, SDK operation registration, snapshot recovery and replay.

The server is required; this acceptance test never silently skips. Each test
owns its task queue and Workflow IDs and uses no Kapy or remote model services.
"""

import asyncio
import os
from datetime import timedelta
from uuid import uuid4

from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from temporal_workflow import PluginWorkflow, agent, client_lookups, discover, rpc_calls
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client
from temporalio.worker import Replayer, Worker


async def test_rpc_activities_restore_snapshots_and_replay_without_rpc():
    client = await Client.connect(
        os.environ.get("PLUGIN_HOST_TEMPORAL_ADDRESS", "127.0.0.1:7233"),
        namespace=os.environ.get("PLUGIN_HOST_TEMPORAL_NAMESPACE", "default"),
        plugins=[PydanticAIPlugin()],
    )
    task_queue = f"plugin-host-{uuid4()}"
    rpc_calls.clear()
    client_lookups.clear()
    async with (
        Worker(
            client,
            task_queue=task_queue,
            workflows=[PluginWorkflow],
            activities=[discover],
            plugins=[AgentPlugin(agent)],
            workflow_failure_exception_types=[AssertionError],
        ),
        asyncio.timeout(60),
    ):
        handles = [
            await client.start_workflow(
                PluginWorkflow.run,
                selected,
                id=f"plugin-host-{uuid4()}",
                task_queue=task_queue,
                execution_timeout=timedelta(seconds=30),
            )
            for selected in (["alpha", "beta"], ["gamma"])
        ]
        outputs = await asyncio.gather(*(handle.result() for handle in handles))
        histories = [await handle.fetch_history() for handle in handles]
    assert outputs == [
        "static alpha\n\nstatic beta\n\ndynamic alpha\n\ndynamic beta",
        "static gamma\n\ndynamic gamma",
    ]
    invokes = [call for call in rpc_calls if call[1] == "plugin.invoke"]
    assert len(client_lookups) == len(invokes) == 12
    assert {params["handler"] for _, _, params in invokes} == {
        "instructions",
        "before_run",
        "before_model_request",
        "after_model_request",
    }
    activities = [
        event.activity_task_scheduled_event_attributes.activity_type.name
        for history in histories
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED
    ]
    rpc_activities = [name for name in activities if "__capability__" in name]
    assert set(rpc_activities) == {
        "agent__plugin_host_integration__capability__selected_plugins__invoke_plugin"
    }
    assert len(rpc_activities) == len(invokes)
    before_replay = (len(rpc_calls), len(client_lookups))
    replayer = Replayer(workflows=[PluginWorkflow], plugins=[PydanticAIPlugin()])
    for history in histories:
        await replayer.replay_workflow(history)
    assert (len(rpc_calls), len(client_lookups)) == before_replay

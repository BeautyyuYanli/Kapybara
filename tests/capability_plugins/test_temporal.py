"""Use a real Temporal service, sandbox and replay; no external model requests.

Set KAPY_TEMPORAL_ADDRESS=temporal:7233 in a container (host default:
localhost:7233). These tests own unique Workflow IDs/task queues.
"""

import asyncio
import json
import os
from collections import Counter
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from temporalio.client import Client, WorkflowFailureError
from temporalio.worker import Replayer, Worker

from capability_plugins.temporal_agent import agent as resource_agent
from capability_plugins.temporal_agent import approval_agent, events
from capability_plugins.temporal_workflow import ApprovalWorkflow, ResourceWorkflow
from kapy.capability_plugins import PluginDeps, PluginSpec
from kapy.capability_plugins.examples.temporal_agent import agent as example_agent
from kapy.capability_plugins.examples.temporal_workflow import PluginExampleWorkflow


async def connect() -> Client:
    return await Client.connect(
        os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("KAPY_TEMPORAL_NAMESPACE", "default"),
        plugins=[PydanticAIPlugin()],
    )


@pytest.mark.asyncio
async def test_temporal_dynamic_toolsets_rebuild_resources_and_replay_without_effects() -> None:
    events.clear()
    client = await connect()
    queue = f"capability-plugins-resource-{uuid4()}"
    inputs = [
        PluginDeps(
            plugins=(
                PluginSpec(plugin_name="echo", config={"prefix": f"{label}:"}),
                PluginSpec(plugin_name="resource", config={"label": label}),
            )
        )
        for label in ("A", "B")
    ] + [PluginDeps()]
    async with Worker(
        client,
        task_queue=queue,
        workflows=[ResourceWorkflow],
        plugins=[AgentPlugin(resource_agent)],
    ):
        handles = await asyncio.gather(
            *(
                client.start_workflow(
                    ResourceWorkflow.run,
                    deps,
                    id=f"{queue}-{index}",
                    task_queue=queue,
                    execution_timeout=timedelta(seconds=45),
                )
                for index, deps in enumerate(inputs)
            )
        )
        assert await asyncio.gather(*(handle.result() for handle in handles)) == [
            "A:hello|tool-hook:A;resource:A:hello|tool-hook:A|hook:A",
            "B:hello|tool-hook:B;resource:B:hello|tool-hook:B|hook:B",
            "disabled",
        ]
        histories = [await handle.fetch_history() for handle in handles]

    counts = Counter(stage for stage, _ in events)
    assert counts["enter"] == counts["exit"] > 0
    assert sorted(label for stage, label in events if stage == "call") == ["A", "B"]
    assert sorted(label for stage, label in events if stage == "validate") == ["A", "B"]
    activity_names = {
        event.activity_task_scheduled_event_attributes.activity_type.name
        for history in histories
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    }
    for operation in ("get_tools", "validate_args", "call_tool"):
        assert (
            f"agent__capability_plugins_test__dynamic_toolset__plugins__{operation}"
            in activity_names
        )

    effects = list(events)
    replayer = Replayer(workflows=[ResourceWorkflow], plugins=[PydanticAIPlugin()])
    for history in histories:
        await replayer.replay_workflow(history)
    assert events == effects


@pytest.mark.asyncio
async def test_temporal_consumer_example_and_invalid_input() -> None:
    client = await connect()
    queue = f"capability-plugins-example-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[PluginExampleWorkflow],
        plugins=[AgentPlugin(example_agent)],
    ):
        deps = PluginDeps(plugins=(PluginSpec(plugin_name="echo", config={"prefix": "Temporal:"}),))
        output = await client.execute_workflow(
            PluginExampleWorkflow.run,
            args=["echo", deps],
            id=f"{queue}-valid",
            task_queue=queue,
            execution_timeout=timedelta(seconds=45),
        )
        assert json.loads(output) == {"example_echo": "Temporal:a"}
        for index, spec in enumerate(
            (
                PluginSpec(plugin_name="unknown"),
                PluginSpec(plugin_name="echo", config={"extra": True}),
            )
        ):
            with pytest.raises(WorkflowFailureError) as caught:
                await client.execute_workflow(
                    PluginExampleWorkflow.run,
                    args=["echo", PluginDeps(plugins=(spec,))],
                    id=f"{queue}-invalid-{index}",
                    task_queue=queue,
                    execution_timeout=timedelta(seconds=45),
                )
            assert getattr(caught.value.cause, "type", None) == "UserError"


@pytest.mark.asyncio
async def test_temporal_preserves_nondefault_tool_definition_after_discovery_activity() -> None:
    client = await connect()
    queue = f"capability-plugins-approval-{uuid4()}"
    deps = PluginDeps(plugins=(PluginSpec(plugin_name="approval"),))
    async with Worker(
        client,
        task_queue=queue,
        workflows=[ApprovalWorkflow],
        plugins=[AgentPlugin(approval_agent)],
    ):
        handle = await client.start_workflow(
            ApprovalWorkflow.run,
            deps,
            id=queue,
            task_queue=queue,
            execution_timeout=timedelta(seconds=45),
        )
        assert json.loads(await handle.result()) == {
            "name": "approved_echo",
            "requires_approval": True,
            "metadata": {"category": "example"},
            "capability_id": "declared_owner",
            "strict": True,
            "sequential": True,
        }
        history = await handle.fetch_history()
    assert any(
        event.HasField("activity_task_scheduled_event_attributes")
        and event.activity_task_scheduled_event_attributes.activity_type.name
        == "agent__capability_plugins_approval__dynamic_toolset__plugins__get_tools"
        for event in history.events
    )

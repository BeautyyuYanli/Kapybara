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

from kapy.layers import LayerDeps, load_layers
from kapy.layers.examples.temporal_agent import agent as example_agent
from kapy.layers.examples.temporal_agent import registry as example_registry
from kapy.layers.examples.temporal_workflow import LayerExampleWorkflow
from layers import tool_definitions
from layers.temporal_agent import agent as resource_agent
from layers.temporal_agent import approval_agent, events, registry, wrapped_agent
from layers.temporal_workflow import ApprovalWorkflow, ResourceWorkflow


async def connect() -> Client:
    return await Client.connect(
        os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("KAPY_TEMPORAL_NAMESPACE", "default"),
        plugins=[PydanticAIPlugin()],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
async def test_temporal_dynamic_toolsets_rebuild_resources_and_replay_without_effects(
    wrapped: bool,
) -> None:
    events.clear()
    client = await connect()
    queue = f"layers-resource-{uuid4()}"
    inputs = [
        LayerDeps(
            layers=load_layers(
                {"echo": {"prefix": f"{label}:"}, "resource": {"label": label}}, {}, registry
            )
        )
        for label in ("A", "B")
    ] + [LayerDeps()]
    async with Worker(
        client,
        task_queue=queue,
        workflows=[ResourceWorkflow],
        plugins=[AgentPlugin(wrapped_agent if wrapped else resource_agent)],
    ):
        handles = await asyncio.gather(
            *(
                client.start_workflow(
                    ResourceWorkflow.run,
                    args=[deps, wrapped],
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
        agent_name = "layers_wrapped_test" if wrapped else "layers_test"
        assert f"agent__{agent_name}__dynamic_toolset__layers__{operation}" in activity_names

    effects = list(events)
    replayer = Replayer(workflows=[ResourceWorkflow], plugins=[PydanticAIPlugin()])
    for history in histories:
        await replayer.replay_workflow(history)
    assert events == effects


@pytest.mark.asyncio
async def test_temporal_consumer_example_and_invalid_input() -> None:
    client = await connect()
    queue = f"layers-example-{uuid4()}"
    async with Worker(
        client,
        task_queue=queue,
        workflows=[LayerExampleWorkflow],
        plugins=[AgentPlugin(example_agent)],
    ):
        deps = LayerDeps(
            layers=load_layers({"echo": {"prefix": "Temporal:"}}, {}, example_registry)
        )
        output = await client.execute_workflow(
            LayerExampleWorkflow.run,
            args=["echo", deps],
            id=f"{queue}-valid",
            task_queue=queue,
            execution_timeout=timedelta(seconds=45),
        )
        assert json.loads(output) == {"example_echo": "Temporal:a"}
        for index, (name, config) in enumerate((("unknown", {}), ("echo", {"extra": True}))):
            with pytest.raises(WorkflowFailureError) as caught:
                await client.execute_workflow(
                    LayerExampleWorkflow.run,
                    args=["echo", LayerDeps(layers={name: {"config": config, "state": {}}})],
                    id=f"{queue}-invalid-{index}",
                    task_queue=queue,
                    execution_timeout=timedelta(seconds=45),
                )
            assert getattr(caught.value.cause, "type", None) == "UserError"


@pytest.mark.asyncio
async def test_temporal_preserves_nondefault_tool_definition_after_discovery_activity() -> None:
    client = await connect()
    queue = f"layers-approval-{uuid4()}"
    deps = LayerDeps(layers=load_layers({"approval": {}}, {}, {"approval": tool_definitions}))
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
        == "agent__layers_approval__dynamic_toolset__layers__get_tools"
        for event in history.events
    )

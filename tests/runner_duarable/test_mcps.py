"""The installed MCPs layer executes in real Activities, saves State and replays without I/O."""

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from temporalio.worker import Replayer, Worker

from kapy.application.resources import connect_temporal
from kapy.control.models.models import ModelRow, ProviderRow
from kapy.control.sessions import CreateSession, SessionService
from kapy.control.sessions.models import SessionRow
from kapy.runner_duarable import RunnerWorkflow, agent, hold_mcp_gateway
from kapy.runner_duarable.activities import RunnerActivities


@pytest.mark.asyncio
@pytest.mark.integration
async def test_registered_mcps_layer_runs_through_gateway_in_activities_and_replays(
    runner_database, tmp_path
):
    marker = tmp_path / "durable-mcp.jsonl"
    config = {
        "servers": {
            "up": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(Path(__file__).parents[1] / "layers/mcp_upstream.py")],
                "env": {"LABEL": "worker", "PID_FILE": str(marker)},
                "credential_env": ["API_KEY"],
            }
        },
        "credentials": {"up": {"env": {"API_KEY": "durable-secret"}}},
    }
    async with runner_database.sessions.begin() as db:
        provider = ProviderRow(
            name="mcp-test",
            api_key="fake-key",
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="runner_duarable.mcps_model:MCPsModel",
        )
        db.add(provider)
        db.add(
            ModelRow(
                provider_id=provider.id, model_name="mcp-test", name="test", context_window=100000
            )
        )
    client = await connect_temporal(runner_database.settings)
    queue = f"mcps-{uuid4()}"
    activities = RunnerActivities(runner_database.sessions, runner_database.outputs)
    async with (
        Worker(
            client, task_queue=queue + "-mcp", activities=[hold_mcp_gateway], identity="mcp-owner"
        ),
        Worker(
            client,
            task_queue=queue,
            workflows=[RunnerWorkflow],
            activities=[activities.record_messages, activities.save_runner_state],
            plugins=[AgentPlugin(agent)],
            identity="agent-worker",
        ),
    ):
        service = SessionService(
            runner_database.sessions, temporal_client=client, temporal_task_queue=queue
        )
        session = await service.create_session(
            CreateSession(
                provider_id=provider.id,
                model_name="mcp-test",
                layers={"mcps": config},
            )
        )
        assert not marker.exists(), "Session creation must not launch an MCP client"
        handle = await service.start_durable_runner(session.id, user_prompt="use MCP")
        assert await handle.result() == "worker:durable:durable-secret"
        history = await handle.fetch_history()
    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, session.id)
        assert row is not None and row.layer_states == {"mcps": {}}
        assert row.layers["mcps"]["credentials"]["up"]["env"]["API_KEY"] == "durable-secret"
        assert row.runner_state_version == 1
    activity_names = {
        event.activity_task_scheduled_event_attributes.activity_type.name
        for event in history.events
        if event.HasField("activity_task_scheduled_event_attributes")
    }
    for operation in ("get_tools", "call_tool"):
        assert f"agent__runner_duarable__dynamic_toolset__mcps__{operation}" in activity_names
    processes = [json.loads(line) for line in marker.read_text().splitlines()]
    assert len({entry["parent"] for entry in processes}) == 1
    assert sum(entry["event"] == "echo" for entry in processes) == 2
    for pid in {entry[key] for entry in processes for key in ("pid", "parent")}:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    before = marker.read_text()
    await Replayer(workflows=[RunnerWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
        history
    )
    assert marker.read_text() == before
    await service.close_session(session.id)

"""Standalone Agent Activities own one stdio Gateway per run and persist empty State."""

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from temporalio.worker import Worker

from kapy.application.resources import connect_temporal
from kapy.control.models.models import ModelRow, ProviderRow
from kapy.control.sessions import CreateSession, SessionService
from kapy.control.sessions.models import SessionRow
from kapy.runner import RunnerActivity


@pytest.mark.asyncio
@pytest.mark.integration
async def test_registered_mcps_layer_owns_gateway_per_run_and_reuses_saved_operation(
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
            model_class="runner.mcps_model:MCPsModel",
        )
        db.add(provider)
        db.add(
            ModelRow(
                provider_id=provider.id, model_name="mcp-test", name="test", context_window=100000
            )
        )
    client = await connect_temporal(runner_database.settings)
    queue = f"mcps-{uuid4()}"
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    async with Worker(
        client,
        task_queue=queue,
        activities=[runner.create, runner.run, runner.close],
    ):
        service = SessionService(
            runner_database.sessions, temporal_client=client, temporal_task_queue=queue
        )
        session = await service.create_session(
            CreateSession(
                provider_id=provider.id,
                model_name="mcp-test",
                layers={"mcps": config},
            ),
            activity_id=uuid4(),
        )
        assert not marker.exists(), "Session creation must not launch an MCP client"
        gateways = set()
        seen = 0
        for _ in range(2):
            key = uuid4()
            operation = await service.start_runner(
                session.id, activity_id=key, user_prompt="use MCP"
            )
            handle = client.get_activity_handle(
                operation.id, run_id=operation.run_id, result_type=str
            )
            assert await handle.result() == "worker:durable:durable-secret"
            async with runner_database.sessions.begin() as db:
                row = await db.get(SessionRow, session.id)
                assert row is not None and row.layer_states == {"mcps": {}}
                assert row.layers["mcps"]["credentials"]["up"]["env"]["API_KEY"] == "durable-secret"
                assert row.runner_state is not None
            processes = [json.loads(line) for line in marker.read_text().splitlines()]
            current = processes[seen:]
            parents = {entry["parent"] for entry in current}
            assert len(parents) == 1 and parents.isdisjoint(gateways)
            assert sum(entry["event"] == "echo" for entry in current) == 2
            gateways.update(parents)
            seen = len(processes)
            for pid in {entry[field] for entry in current for field in ("pid", "parent")}:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
            before = marker.read_text()
            confirmed = await service.start_runner(
                session.id, activity_id=key, user_prompt="use MCP"
            )
            assert confirmed.run_id == operation.run_id
            assert await confirmed.result() == "worker:durable:durable-secret"
            assert marker.read_text() == before
        await service.close_session(session.id, activity_id=uuid4())

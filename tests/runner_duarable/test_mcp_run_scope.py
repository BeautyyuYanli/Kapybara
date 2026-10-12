"""Real run owners share one pipe across calls, close on errors, and recover via Temporal."""

import asyncio
import json
import os
import sys
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import is_cancelled_exception
from temporalio.worker import Worker

from kapy.application.resources import connect_temporal
from kapy.control.models.models import ModelRow, ProviderRow
from kapy.control.sessions import CreateSession, SessionService
from kapy.control.sessions.models import SessionRow
from kapy.layers.mcps.models import GatewayEndpoint
from kapy.runner_duarable import RunnerWorkflow, agent, hold_mcp_gateway
from kapy.runner_duarable.activities import RunnerActivities
from kapy.runner_duarable.mcp_gateway import READY_SIGNAL


def upstream(label, marker):
    return {
        "servers": {
            "up": {
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(Path(__file__).parents[1] / "layers/mcp_upstream.py")],
                "env": {"LABEL": label, "PID_FILE": str(marker)},
                "credential_env": ["API_KEY"],
            }
        },
        "credentials": {"up": {"env": {"API_KEY": f"{label}-secret"}}},
    }


async def setup(database):
    async with database.sessions.begin() as db:
        provider = ProviderRow(
            name="mcp-scope-test",
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
    client = await connect_temporal(database.settings)
    queue = f"mcps-{uuid4()}"
    service = SessionService(database.sessions, temporal_client=client, temporal_task_queue=queue)
    return client, queue, service, provider.id


def main_worker(client, queue, database):
    acts = RunnerActivities(database.sessions, database.outputs)
    return Worker(
        client,
        task_queue=queue,
        workflows=[RunnerWorkflow],
        activities=[acts.record_messages, acts.save_runner_state],
        plugins=[AgentPlugin(agent)],
        identity="agent-worker",
        graceful_shutdown_timeout=timedelta(seconds=0),
    )


async def endpoints(client, history):
    values = []
    for event in history.events:
        if not event.HasField("workflow_execution_signaled_event_attributes"):
            continue
        signal = event.workflow_execution_signaled_event_attributes
        if signal.signal_name == READY_SIGNAL:
            (value,) = await client.config(active_config=True)["data_converter"].decode(
                list(signal.input.payloads), [GatewayEndpoint]
            )
            assert isinstance(value, GatewayEndpoint)
            values.append(value)
    return values


def process_entries(marker):
    return [json.loads(line) for line in marker.read_text().splitlines()]


def assert_closed(marker):
    entries = process_entries(marker)
    for pid in {entry[key] for entry in entries for key in ("pid", "parent")}:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


async def assert_relay_closed(endpoint):
    async with httpx2.AsyncClient(timeout=2, trust_env=False) as http:
        with pytest.raises(httpx2.ConnectError):
            await http.get(str(endpoint.url))


@asynccontextmanager
async def separate_owner(queue):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).with_name("mcp_owner_worker.py")),
        queue,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        assert process.stdout is not None
        assert await asyncio.wait_for(process.stdout.readline(), 30) == b"ready\n"
        yield process
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 20)
            except TimeoutError:
                process.kill()
                await process.wait()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_concurrent_runs_are_isolated_and_following_run_uses_new_gateway(
    runner_database, tmp_path
):
    client, queue, service, provider = await setup(runner_database)
    markers = [tmp_path / f"{label}.jsonl" for label in ("alice", "bob")]
    sessions = [
        await service.create_session(
            CreateSession(
                provider_id=provider,
                model_name="mcp-test",
                layers={"mcps": upstream(label, marker)},
            )
        )
        for label, marker in zip(("alice", "bob"), markers, strict=True)
    ]
    async with (
        Worker(
            client, task_queue=queue + "-mcp", activities=[hold_mcp_gateway], identity="mcp-owner"
        ),
        main_worker(client, queue, runner_database),
    ):
        handles = [
            await service.start_durable_runner(session.id, user_prompt="twice")
            for session in sessions
        ]
        assert await asyncio.gather(*(handle.result() for handle in handles)) == [
            "alice:durable:alice-secret",
            "bob:durable:bob-secret",
        ]
        catalogs = [await endpoints(client, await handle.fetch_history()) for handle in handles]
        assert all(len(catalog) == 1 for catalog in catalogs)
        assert catalogs[0][0].token != catalogs[1][0].token
        parents = [{entry["parent"] for entry in process_entries(marker)} for marker in markers]
        assert all(len(values) == 1 for values in parents) and parents[0].isdisjoint(parents[1])
        for marker, catalog in zip(markers, catalogs, strict=True):
            assert_closed(marker)
            await assert_relay_closed(catalog[0])
        offset = len(process_entries(markers[0]))
        again = await service.start_durable_runner(sessions[0].id, user_prompt="again")
        assert await again.result() == "alice:durable:alice-secret"
        following = {entry["parent"] for entry in process_entries(markers[0])[offset:]}
        assert len(following) == 1 and following.isdisjoint(parents[0])
        assert_closed(markers[0])
    for session in sessions:
        await service.close_session(session.id)


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize("prompt", ["fail", "block"])
async def test_tool_failure_and_workflow_cancellation_close_run_owner(
    runner_database, tmp_path, prompt
):
    client, queue, service, provider = await setup(runner_database)
    marker = tmp_path / f"{prompt}.jsonl"
    session = await service.create_session(
        CreateSession(
            provider_id=provider, model_name="mcp-test", layers={"mcps": upstream(prompt, marker)}
        )
    )
    async with (
        Worker(client, task_queue=queue + "-mcp", activities=[hold_mcp_gateway]),
        main_worker(client, queue, runner_database),
    ):
        handle = await service.start_durable_runner(session.id, user_prompt=prompt)
        if prompt == "block":
            async with asyncio.timeout(60):
                while True:
                    if marker.exists() and any(
                        entry["event"] == "block" for entry in process_entries(marker)
                    ):
                        break
                    await asyncio.sleep(0.02)
            (endpoint,) = await endpoints(client, await handle.fetch_history())
            async with httpx2.AsyncClient(timeout=2, trust_env=False) as http:
                assert (await http.get(str(endpoint.url))).status_code == 401
            await handle.cancel()
        with pytest.raises(WorkflowFailureError) as exc:
            await asyncio.wait_for(handle.result(), 60)
        assert is_cancelled_exception(exc.value.cause) == (prompt == "block")
        (endpoint,) = await endpoints(client, await handle.fetch_history())
        assert_closed(marker)
        await assert_relay_closed(endpoint)
    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, session.id)
        assert row is not None and row.runner_state_version == 0
        assert row.layer_states == {"mcps": {}}
    await service.close_session(session.id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_owner_worker_restart_replaces_gateway_without_replaying_successful_tool_call(
    runner_database, tmp_path, monkeypatch
):
    monkeypatch.setenv("KAPY_MCP_TEST_GATE_DIR", str(tmp_path))
    client, queue, service, provider = await setup(runner_database)
    marker = tmp_path / "recover.jsonl"
    session = await service.create_session(
        CreateSession(
            provider_id=provider,
            model_name="mcp-test",
            layers={"mcps": upstream("recover", marker)},
        )
    )
    async with main_worker(client, queue, runner_database):
        async with separate_owner(queue) as retiring:
            handle = await service.start_durable_runner(session.id, user_prompt="restart")
            async with asyncio.timeout(60):
                while True:
                    if (tmp_path / "waiting.json").exists():
                        break
                    await asyncio.sleep(0.02)
            retiring.terminate()
            assert await asyncio.wait_for(retiring.wait(), 20) == 0
            assert_closed(marker)
        async with separate_owner(queue):
            async with asyncio.timeout(60):
                while True:
                    recorded = await endpoints(client, await handle.fetch_history())
                    if len(recorded) >= 2:
                        break
                    await asyncio.sleep(0.05)
            assert [endpoint.attempt for endpoint in recorded] == [1, 2]
            assert recorded[0].token != recorded[1].token
            await assert_relay_closed(recorded[0])
            (tmp_path / "resume").touch()
            assert await asyncio.wait_for(handle.result(), 60) == "recover:durable:recover-secret"
            entries = process_entries(marker)
            assert sum(entry["event"] == "echo" for entry in entries) == 2
            assert len({entry["parent"] for entry in entries}) == 2
            assert_closed(marker)
            await assert_relay_closed(recorded[1])
    await service.close_session(session.id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_agent_worker_restart_keeps_separate_run_owner_and_does_not_repeat_calls(
    runner_database, tmp_path, monkeypatch
):
    monkeypatch.setenv("KAPY_MCP_TEST_GATE_DIR", str(tmp_path))
    client, queue, service, provider = await setup(runner_database)
    marker = tmp_path / "worker-restart.jsonl"
    session = await service.create_session(
        CreateSession(
            provider_id=provider,
            model_name="mcp-test",
            layers={"mcps": upstream("restart", marker)},
        )
    )
    async with separate_owner(queue):
        working = main_worker(client, queue, runner_database)
        async with working:
            handle = await service.start_durable_runner(session.id, user_prompt="restart")
            async with asyncio.timeout(60):
                while True:
                    if (tmp_path / "waiting.json").exists():
                        break
                    await asyncio.sleep(0.02)
            before = {entry["parent"] for entry in process_entries(marker)}
            assert len(before) == 1
            await working.shutdown()
            for pid in before:
                os.kill(pid, 0)
        async with main_worker(client, queue, runner_database):
            (tmp_path / "resume").touch()
            assert await asyncio.wait_for(handle.result(), 60) == "restart:durable:restart-secret"
            entries = process_entries(marker)
            assert {entry["parent"] for entry in entries} == before
            assert sum(entry["event"] == "echo" for entry in entries) == 2
            assert_closed(marker)
            recorded = await endpoints(client, await handle.fetch_history())
            assert len(recorded) == 1
            await assert_relay_closed(recorded[0])
    await service.close_session(session.id)

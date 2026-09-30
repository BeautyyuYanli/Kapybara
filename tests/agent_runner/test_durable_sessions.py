"""Durable admission uses PostgreSQL locks and actual Temporal execution identities."""

import asyncio
import os
from contextlib import suppress
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.service import RPCError, RPCStatusCode

from kapy.agent_plugins import AgentPluginService, PluginSpec
from kapy.agent_runner.models import AgentStateRow
from kapy.application.agent import create_registry
from kapy.control.models import ModelService
from kapy.control.sessions import CreateSession, DurableRunnerConflict, SessionService
from kapy.interfaces.http import create_router
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.session_lease import SessionBusy, open_session_lease

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def client():
    return await Client.connect(
        os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"), plugins=[PydanticAIPlugin()]
    )


async def test_real_temporal_admission_preserves_queues_and_cancel_without_lease(
    database, seed_session
):
    temporal = await client()
    session_id = uuid4()
    await seed_session(session_id)
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    assert not await sessions.is_durable_runner_running(session_id)
    await sessions.enqueue_input(session_id, "queued", "keep queue")
    await sessions.request_cancel(session_id)
    # A legacy lease alone is not a durable runner dependency.
    async with open_session_lease(session_id, session_factory=database.sessions):
        handle = await sessions.start_durable_runner(session_id, user_prompt="direct prompt")
    try:
        assert handle.id == f"kapy-runner:{session_id}" and handle.result_run_id
        assert await sessions.is_durable_runner_running(session_id)
        assert not await sessions.is_runner_running(session_id)
        with pytest.raises(SessionBusy):
            await sessions.start_durable_runner(session_id, user_prompt="busy")
        with pytest.raises(SessionBusy):
            await sessions.close_session(session_id)
        assert (await sessions.get_session(session_id)).status == LifecycleStatus.READY
        assert (await sessions.read_inputs(session_id, "queued"))[0].content == "keep queue"
        assert await sessions.read_cancel(session_id)
    finally:
        await handle.terminate("test cleanup")
    assert not await sessions.is_durable_runner_running(session_id)
    with pytest.raises(DurableRunnerConflict):
        await sessions.start_durable_runner(session_id, user_prompt="do not restart old state")
    await sessions.close_session(session_id)
    with pytest.raises(LifecycleError):
        await sessions.start_durable_runner(session_id, user_prompt="closed")


async def test_concurrent_starts_admit_one_execution(database, seed_session):
    temporal = await client()
    session_id = uuid4()
    await seed_session(session_id)
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    results = await asyncio.gather(
        *(sessions.start_durable_runner(session_id, user_prompt=text) for text in ["one", "two"]),
        return_exceptions=True,
    )
    handles = [result for result in results if not isinstance(result, BaseException)]
    try:
        assert len(handles) == 1
        assert sum(isinstance(result, SessionBusy) for result in results) == 1
    finally:
        for handle in handles:
            await handle.terminate("test cleanup")


async def test_close_serializes_with_inflight_start_confirmation(database, seed_session):
    temporal = await client()
    session_id = uuid4()
    await seed_session(session_id)
    accepted, release = asyncio.Event(), asyncio.Event()
    original = temporal.start_workflow
    handles = []

    async def held_start(*args, **kwargs):
        handle = await original(*args, **kwargs)
        handles.append(handle)
        accepted.set()
        await release.wait()
        return handle

    temporal.start_workflow = held_start
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    start = asyncio.create_task(sessions.start_durable_runner(session_id, user_prompt="one"))
    close = None
    try:
        await asyncio.wait_for(accepted.wait(), 5)
        close = asyncio.create_task(sessions.close_session(session_id))
        await asyncio.sleep(0.05)
        assert not close.done()
        release.set()
        await start
        with pytest.raises(SessionBusy):
            await close
        assert (await sessions.get_session(session_id)).status == LifecycleStatus.READY
    finally:
        release.set()
        for task in (start, close):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError, SessionBusy):
                    await task
        for handle in handles:
            await handle.terminate("test cleanup")


async def test_legacy_checkpoint_is_rejected_before_temporal_lookup(database, seed_session):
    session_id = uuid4()
    await seed_session(session_id)
    async with database.sessions.begin() as db:
        db.add(AgentStateRow(session_id=session_id))
    temporal = Mock(spec=Client)
    sessions = SessionService(database.sessions, temporal_client=temporal, temporal_task_queue="q")
    with pytest.raises(ValueError, match="Legacy checkpoints"):
        await sessions.start_durable_runner(session_id, user_prompt="hello")
    temporal.get_workflow_handle.assert_not_called()


@pytest.mark.parametrize("status", [RPCStatusCode.UNAVAILABLE, RPCStatusCode.DEADLINE_EXCEEDED])
async def test_temporal_rpc_failures_are_not_idle_or_leaked_over_http(
    database, seed_session, status
):
    session_id = uuid4()
    await seed_session(session_id)
    temporal = Mock(spec=Client)
    temporal.get_workflow_handle.return_value.describe = AsyncMock(
        side_effect=RPCError("secret-provider-payload", status, b"")
    )
    sessions = SessionService(database.sessions, temporal_client=temporal, temporal_task_queue="q")
    with pytest.raises(RPCError):
        await sessions.is_durable_runner_running(session_id)
    with pytest.raises(RPCError):
        await sessions.close_session(session_id)
    assert (await sessions.get_session(session_id)).status == LifecycleStatus.READY
    app = FastAPI()
    app.include_router(create_router(ModelService(database.sessions), sessions))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as http:
        for result in (
            await http.get(f"/api/sessions/{session_id}/runner"),
            await http.post(f"/api/sessions/{session_id}/runner", json={"user_prompt": "hello"}),
        ):
            assert result.status_code == 503
            assert "secret" not in result.text
        assert (
            await http.post(f"/api/sessions/{session_id}/inputs", json={"content": "q"})
        ).status_code == 202
        assert (await http.post(f"/api/sessions/{session_id}/cancel")).status_code == 202
    temporal.start_workflow.assert_not_called()


async def test_http_start_returns_temporal_run_identity_and_busy_conflicts(database, seed_session):
    session_id = uuid4()
    await seed_session(session_id)
    temporal = await client()
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    app = FastAPI()
    app.include_router(create_router(ModelService(database.sessions), sessions))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            path = f"/api/sessions/{session_id}/runner"
            assert (await http.get(path)).json() is False
            assert (await http.post(path, json={"user_prompt": ["not text"]})).status_code == 422
            result = await http.post(path, json={"user_prompt": "hello"})
            assert result.status_code == 202
            data = result.json()
            description = await temporal.get_workflow_handle(data["workflow_id"]).describe()
            assert data == {
                "workflow_id": f"kapy-runner:{session_id}",
                "run_id": description.run_id,
            }
            assert (await http.get(path)).json() is True
            assert (await http.post(path, json={"user_prompt": "busy"})).status_code == 409
            await temporal.get_workflow_handle(data["workflow_id"]).terminate("test terminal")
            assert (await http.post(path, json={"user_prompt": "failed"})).status_code == 409
    finally:
        handle = temporal.get_workflow_handle(f"kapy-runner:{session_id}")
        if (await handle.describe()).status == WorkflowExecutionStatus.RUNNING:
            await handle.terminate("test cleanup")


@pytest.mark.parametrize("failure", ["rpc", "cancelled"])
async def test_accepted_start_survives_local_confirmation_failure(
    database, seed_session, monkeypatch, failure
):
    temporal = await client()
    session_id = uuid4()
    await seed_session(session_id)
    original = temporal.start_workflow
    accepted, release = asyncio.Event(), asyncio.Event()
    handles = []
    attempts = 0
    rpc_failure = RPCError("start confirmation lost", RPCStatusCode.DEADLINE_EXCEEDED, b"")

    async def unconfirmed_start(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        handle = await original(*args, **kwargs)
        handles.append(handle)
        accepted.set()
        await release.wait()
        raise rpc_failure

    monkeypatch.setattr(temporal, "start_workflow", unconfirmed_start)
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    start = asyncio.create_task(sessions.start_durable_runner(session_id, user_prompt="one"))
    try:
        await asyncio.wait_for(accepted.wait(), 5)
        if failure == "cancelled":
            start.cancel()
            with pytest.raises(asyncio.CancelledError):
                await start
        else:
            release.set()
            with pytest.raises(RPCError) as caught:
                await start
            assert caught.value is rpc_failure
        assert (await handles[0].describe()).status == WorkflowExecutionStatus.RUNNING
        assert await sessions.is_durable_runner_running(session_id)
        with pytest.raises(SessionBusy):
            await sessions.start_durable_runner(session_id, user_prompt="another prompt")
        with pytest.raises(SessionBusy):
            await sessions.close_session(session_id)
        assert attempts == 1
        assert (await sessions.get_session(session_id)).status == LifecycleStatus.READY
    finally:
        start.cancel()
        await asyncio.gather(start, return_exceptions=True)
        for handle in handles:
            await handle.terminate("test cleanup")


async def test_business_plugin_binding_rejects_durable_start_without_changing_config(
    database, seed_session
):
    temporal = await client()
    existing_id = uuid4()
    await seed_session(existing_id)
    plugins = AgentPluginService(database.sessions, create_registry())
    sessions = SessionService(
        database.sessions,
        temporal_client=temporal,
        temporal_task_queue=f"unpolled-{uuid4()}",
        plugin_service=plugins,
    )
    existing = await sessions.get_session(existing_id)
    spec = PluginSpec(
        plugin_provider="builtin",
        plugin_name="response_rewrite",
        config={
            "prompt": "Keep the reply concise.",
            "base_url": "http://127.0.0.1:9999/",
            "api_key": "unused-test-key",
        },
    )
    session = await sessions.create_session(
        CreateSession(
            provider_id=existing.provider_id, model_name=existing.model_name, plugins=[spec]
        )
    )
    original_bindings = await plugins.list_bindings(session.id)
    assert len(original_bindings) == 1 and original_bindings[0].config == spec.config
    try:
        with pytest.raises(ValueError, match="business plugins"):
            await sessions.start_durable_runner(session.id, user_prompt="not supported")
        assert await plugins.list_bindings(session.id) == original_bindings
        assert (await sessions.get_session(session.id)).status == LifecycleStatus.READY
        assert not await sessions.is_durable_runner_running(session.id)
    finally:
        if await sessions.is_durable_runner_running(session.id):
            await temporal.get_workflow_handle(f"kapy-runner:{session.id}").terminate(
                "test cleanup"
            )

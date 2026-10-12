"""Durable admission uses PostgreSQL locks and actual Temporal execution identities."""

import asyncio
import os
from contextlib import suppress
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from temporalio.client import ActivityExecutionDescription, ActivityExecutionStatus, Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.service import RPCError, RPCStatusCode

from kapy.control.models import ModelService
from kapy.control.sessions import RunnerConflict, SessionService
from kapy.control.sessions.models import SessionRow
from kapy.interfaces.http import create_router
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.session_lease import SessionBusy, open_session_lease

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def client():
    return await Client.connect(
        os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
        data_converter=pydantic_data_converter,
    )


@pytest.mark.parametrize(
    "phase", [LifecycleStatus.CREATING, LifecycleStatus.CLOSING, LifecycleStatus.CLOSED]
)
async def test_non_ready_session_rejects_execution_before_temporal_calls(
    database, seed_session, phase
):
    session_id = uuid4()
    await seed_session(session_id)
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        assert row is not None
        row.status = phase
    temporal = Mock(spec=Client)
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue="unused"
    )
    with pytest.raises(LifecycleError):
        await sessions.start_runner(session_id, user_prompt="do not execute")
    temporal.get_activity_handle.assert_not_called()
    temporal.start_activity.assert_not_called()


async def test_real_temporal_admission_and_status_use_activity_without_execution_lease(
    database, seed_session
):
    temporal = await client()
    session_id = uuid4()
    await seed_session(session_id)
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    assert not await sessions.is_runner_running(session_id)
    # A scope lease is not an execution dependency or evidence of a running Activity.
    async with open_session_lease(session_id, session_factory=database.sessions):
        handle = await sessions.start_runner(session_id, user_prompt="direct prompt")
    try:
        assert handle.id == f"kapy-runner:{session_id}" and handle.run_id
        assert await sessions.is_runner_running(session_id)
        with pytest.raises(SessionBusy):
            await sessions.start_runner(session_id, user_prompt="busy")
        with pytest.raises(SessionBusy):
            await sessions.close_session(session_id)
        assert (await sessions.get_session(session_id)).status == LifecycleStatus.READY
    finally:
        await handle.terminate(reason="test cleanup")
    assert not await sessions.is_runner_running(session_id)
    with pytest.raises(RunnerConflict):
        await sessions.start_runner(session_id, user_prompt="do not restart old state")
    await sessions.close_session(session_id)
    with pytest.raises(LifecycleError):
        await sessions.start_runner(session_id, user_prompt="closed")


async def test_concurrent_starts_admit_one_execution(database, seed_session):
    temporal = await client()
    session_id = uuid4()
    await seed_session(session_id)
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    results = await asyncio.gather(
        *(sessions.start_runner(session_id, user_prompt=text) for text in ["one", "two"]),
        return_exceptions=True,
    )
    handles = [result for result in results if not isinstance(result, BaseException)]
    try:
        assert len(handles) == 1
        assert sum(isinstance(result, SessionBusy) for result in results) == 1
    finally:
        for handle in handles:
            await handle.terminate(reason="test cleanup")


async def test_close_serializes_with_inflight_start_confirmation(database, seed_session):
    temporal = await client()
    session_id = uuid4()
    await seed_session(session_id)
    accepted, release = asyncio.Event(), asyncio.Event()
    original = temporal.start_activity
    handles = []

    async def held_start(*args, **kwargs):
        handle = await original(*args, **kwargs)
        handles.append(handle)
        accepted.set()
        await release.wait()
        return handle

    temporal.start_activity = held_start
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    start = asyncio.create_task(sessions.start_runner(session_id, user_prompt="one"))
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
            await handle.terminate(reason="test cleanup")


@pytest.mark.parametrize("status", [RPCStatusCode.UNAVAILABLE, RPCStatusCode.DEADLINE_EXCEEDED])
async def test_temporal_rpc_failures_are_not_idle_or_leaked_over_http(
    database, seed_session, status
):
    session_id = uuid4()
    await seed_session(session_id)
    temporal = Mock(spec=Client)
    temporal.get_activity_handle.return_value.describe = AsyncMock(
        side_effect=RPCError("secret-provider-payload", status, b"")
    )
    sessions = SessionService(database.sessions, temporal_client=temporal, temporal_task_queue="q")
    with pytest.raises(RPCError):
        await sessions.is_runner_running(session_id)
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
    temporal.start_activity.assert_not_called()


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
            description = await temporal.get_activity_handle(data["activity_id"]).describe()
            assert data == {
                "activity_id": f"kapy-runner:{session_id}",
                "run_id": description.activity_run_id,
            }
            assert (await http.get(path)).json() is True
            assert (await http.post(path, json={"user_prompt": "busy"})).status_code == 409
            await temporal.get_activity_handle(data["activity_id"]).terminate(
                reason="test terminal"
            )
            assert (await http.post(path, json={"user_prompt": "failed"})).status_code == 409
    finally:
        handle = temporal.get_activity_handle(f"kapy-runner:{session_id}")
        if (await handle.describe()).status == ActivityExecutionStatus.RUNNING:
            await handle.terminate(reason="test cleanup")


@pytest.mark.parametrize("failure", ["rpc", "cancelled"])
async def test_accepted_start_survives_local_confirmation_failure(
    database, seed_session, monkeypatch, failure
):
    temporal = await client()
    session_id = uuid4()
    await seed_session(session_id)
    original = temporal.start_activity
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

    monkeypatch.setattr(temporal, "start_activity", unconfirmed_start)
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    start = asyncio.create_task(sessions.start_runner(session_id, user_prompt="one"))
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
        assert (await handles[0].describe()).status == ActivityExecutionStatus.RUNNING
        assert await sessions.is_runner_running(session_id)
        with pytest.raises(SessionBusy):
            await sessions.start_runner(session_id, user_prompt="another prompt")
        with pytest.raises(SessionBusy):
            await sessions.close_session(session_id)
        assert attempts == 1
        assert (await sessions.get_session(session_id)).status == LifecycleStatus.READY
    finally:
        start.cancel()
        await asyncio.gather(start, return_exceptions=True)
        for handle in handles:
            await handle.terminate(reason="test cleanup")


async def test_paused_activity_still_owns_session_admission(database, seed_session):
    session_id = uuid4()
    await seed_session(session_id)
    # Pausing is an optional server operator API. Its SDK status still represents
    # an existing execution and must not permit closing the underlying scope.
    temporal = Mock(spec=Client)
    temporal.get_activity_handle.return_value.describe = AsyncMock(
        return_value=Mock(spec=ActivityExecutionDescription, status=ActivityExecutionStatus.PAUSED)
    )
    sessions = SessionService(
        database.sessions, temporal_client=temporal, temporal_task_queue="unused"
    )
    assert await sessions.is_runner_running(session_id)
    with pytest.raises(SessionBusy):
        await sessions.start_runner(session_id, user_prompt="busy")
    with pytest.raises(SessionBusy):
        await sessions.close_session(session_id)
    assert (await sessions.get_session(session_id)).status == LifecycleStatus.READY
    temporal.start_activity.assert_not_called()

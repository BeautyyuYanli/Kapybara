"""Real database ownership and native Temporal identity across admission races."""

import asyncio
from datetime import timedelta
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from temporalio.service import RPCError, RPCStatusCode

from kapy.control.models import ModelService
from kapy.control.sessions import (
    OperationConflict,
    OperationPhase,
    SessionBusy,
    SessionService,
    UpdateSession,
)
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.operations import SessionOperationRow
from kapy.control.types import utc_now
from kapy.interfaces.http import create_router
from kapy.lifecycle import LifecycleError, LifecycleStatus

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_queued_run_owns_session_and_reuses_original_execution(
    database, seed_session, session_runtime
):
    session_id, activity_id = uuid4(), uuid4()
    await seed_session(session_id)
    client = session_runtime.client
    sessions = SessionService(
        database.sessions, temporal_client=client, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    first = await sessions.start_runner(session_id, activity_id=activity_id, user_prompt="original")
    try:
        second = await sessions.start_runner(
            session_id, activity_id=activity_id, user_prompt="original"
        )
        assert first == second and first.phase == OperationPhase.PENDING and first.run_id
        assert (await sessions.get_session(session_id)).status == LifecycleStatus.STARTING
        assert await sessions.is_runner_running(session_id)
        with pytest.raises(OperationConflict):
            await sessions.start_runner(session_id, activity_id=activity_id, user_prompt="changed")
        with pytest.raises(SessionBusy):
            await sessions.start_runner(session_id, activity_id=uuid4(), user_prompt="another")
        with pytest.raises(SessionBusy):
            await sessions.close_session(session_id, activity_id=uuid4())
        with pytest.raises(LifecycleError):
            await sessions.update_session(session_id, UpdateSession(title="busy"))
        assert (
            await client.get_activity_handle(str(activity_id)).describe()
        ).activity_run_id == first.run_id
    finally:
        await client.get_activity_handle(str(activity_id), run_id=first.run_id).terminate(
            reason="test cleanup"
        )
    ended = await sessions.get_operation(session_id, activity_id)
    assert ended.phase == OperationPhase.FAILED
    assert (await sessions.get_session(session_id)).status == LifecycleStatus.CLOSING
    assert not await sessions.is_runner_running(session_id)
    cleanup = session_runtime.service(database.sessions)
    assert (
        await cleanup.close_session(session_id, activity_id=uuid4())
    ).status == LifecycleStatus.CLOSED
    assert (
        await sessions.start_runner(session_id, activity_id=activity_id, user_prompt="original")
        == ended
    )


@pytest.mark.parametrize("same_key", [False, True])
async def test_concurrent_submission_has_one_owner(
    database, seed_session, session_runtime, same_key
):
    session_id, key = uuid4(), uuid4()
    await seed_session(session_id)
    sessions = SessionService(
        database.sessions,
        temporal_client=session_runtime.client,
        temporal_task_queue=f"unpolled-{uuid4()}",
    )
    results = await asyncio.gather(
        *(
            sessions.start_runner(
                session_id, activity_id=key if same_key else uuid4(), user_prompt="same"
            )
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    operations = [result for result in results if not isinstance(result, BaseException)]
    try:
        assert len(operations) == (2 if same_key else 1)
        assert len({operation.activity_id for operation in operations}) == 1
        if same_key:
            assert operations[0].run_id == operations[1].run_id
        else:
            assert sum(isinstance(result, SessionBusy) for result in results) == 1
    finally:
        for key in {operation.activity_id for operation in operations}:
            await session_runtime.client.get_activity_handle(str(key)).terminate(
                reason="test cleanup"
            )


@pytest.mark.parametrize("failure", ["rpc", "cancelled"])
async def test_uncertain_start_keeps_ownership_and_same_key_confirms(
    database, seed_session, session_runtime, monkeypatch, failure
):
    session_id, key = uuid4(), uuid4()
    await seed_session(session_id)
    client = session_runtime.client
    original = client.start_activity
    accepted, release = asyncio.Event(), asyncio.Event()
    handles = []

    async def uncertain(*args, **kwargs):
        handles.append(await original(*args, **kwargs))
        accepted.set()
        await release.wait()
        raise RPCError("secret confirmation failure", RPCStatusCode.DEADLINE_EXCEEDED, b"")

    monkeypatch.setattr(client, "start_activity", uncertain)
    sessions = SessionService(
        database.sessions, temporal_client=client, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    task = asyncio.create_task(
        sessions.start_runner(session_id, activity_id=key, user_prompt="original")
    )
    try:
        await asyncio.wait_for(accepted.wait(), 5)
        # The database connection is free while submission is awaiting RPC.
        async with database.sessions.begin() as db:
            assert (await db.get(SessionRow, session_id)).status == LifecycleStatus.STARTING
        with pytest.raises(SessionBusy):
            await sessions.close_session(session_id, activity_id=uuid4())
        if failure == "cancelled":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            with pytest.raises(RPCError):
                await task
        monkeypatch.setattr(client, "start_activity", original)
        confirmed = await sessions.start_runner(session_id, activity_id=key, user_prompt="original")
        assert confirmed.run_id == handles[0].run_id
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for handle in handles:
            await handle.terminate(reason="test cleanup")


async def test_expired_unconfirmed_operation_is_terminal_and_does_not_resubmit(
    database, seed_session, session_runtime, monkeypatch
):
    session_id, key = uuid4(), uuid4()
    await seed_session(session_id)
    sessions = SessionService(
        database.sessions,
        temporal_client=session_runtime.client,
        temporal_task_queue=f"unpolled-{uuid4()}",
    )
    operation = await sessions.start_runner(session_id, activity_id=key, user_prompt="original")
    await session_runtime.client.get_activity_handle(str(key)).terminate(reason="test cleanup")
    async with database.sessions.begin() as db:
        row = await db.get(SessionOperationRow, key)
        row.deadline = utc_now() - timedelta(seconds=1)
    monkeypatch.setattr(
        session_runtime.client,
        "start_activity",
        Mock(side_effect=AssertionError("must not resurrect")),
    )
    ended = await sessions.start_runner(session_id, activity_id=key, user_prompt="original")
    assert ended.phase == OperationPhase.FAILED and ended.run_id == operation.run_id
    assert (await sessions.get_session(session_id)).status == LifecycleStatus.CLOSING


async def test_http_requires_keys_returns_public_operation_and_enforces_intent(
    database, seed_session, session_runtime
):
    session_id, key = uuid4(), uuid4()
    await seed_session(session_id)
    sessions = SessionService(
        database.sessions,
        temporal_client=session_runtime.client,
        temporal_task_queue=f"unpolled-{uuid4()}",
    )
    app = FastAPI()
    app.include_router(create_router(ModelService(database.sessions), sessions))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            path = f"/api/sessions/{session_id}/runner"
            assert (await http.post(path, json={"user_prompt": "hello"})).status_code == 422
            body = {"activity_id": str(key), "user_prompt": "hello"}
            response = await http.post(path, json=body)
            assert response.status_code == 202
            data = response.json()
            assert data["activity_id"] == str(key) and data["phase"] == "pending" and data["run_id"]
            assert "input" not in data and "request" not in data and "test-key" not in response.text
            assert (await http.post(path, json=body)).json() == data
            assert (
                await http.post(path, json=body | {"user_prompt": "different"})
            ).status_code == 409
            assert (await http.get(f"/api/sessions/{session_id}/operations/{key}")).json() == data
            assert (await http.get(f"/api/sessions/{uuid4()}/operations/{key}")).status_code == 404
    finally:
        await session_runtime.client.get_activity_handle(str(key)).terminate(reason="test cleanup")


@pytest.mark.parametrize("query_failure", ["not_found", "unavailable"])
async def test_unknown_execution_keeps_owner_until_fixed_deadline(
    database, seed_session, session_runtime, monkeypatch, query_failure
):
    from unittest.mock import AsyncMock

    session_id, key = uuid4(), uuid4()
    await seed_session(session_id)
    client = session_runtime.client
    sessions = SessionService(
        database.sessions, temporal_client=client, temporal_task_queue=f"unpolled-{uuid4()}"
    )
    # No execution is accepted, but the durable pending owner already exists.
    start = AsyncMock(
        side_effect=RPCError("submission unavailable", RPCStatusCode.UNAVAILABLE, b"")
    )
    monkeypatch.setattr(client, "start_activity", start)
    with pytest.raises(RPCError):
        await sessions.start_runner(session_id, activity_id=key, user_prompt="original")
    handle = client.get_activity_handle(str(key))
    with pytest.raises(RPCError) as missing:
        await handle.describe()
    assert missing.value.status == RPCStatusCode.NOT_FOUND
    if query_failure == "unavailable":
        monkeypatch.setattr(
            handle,
            "describe",
            AsyncMock(side_effect=RPCError("query unavailable", RPCStatusCode.UNAVAILABLE, b"")),
        )
        monkeypatch.setattr(client, "get_activity_handle", lambda *args, **kwargs: handle)
    pending = await sessions.get_operation(session_id, key)
    assert pending.phase == OperationPhase.PENDING and pending.run_id is None
    assert (await sessions.get_session(session_id)).status == LifecycleStatus.STARTING
    assert await sessions.is_runner_running(session_id)
    with pytest.raises(SessionBusy):
        await sessions.start_runner(session_id, activity_id=uuid4(), user_prompt="other")
    with pytest.raises(SessionBusy):
        await sessions.close_session(session_id, activity_id=uuid4())
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        operation = await db.get(SessionOperationRow, key)
        assert row.current_activity_id == key and operation.deadline == pending.deadline
        operation.deadline = utc_now() - timedelta(seconds=1)
    terminal = await sessions.get_operation(session_id, key)
    assert terminal.phase == OperationPhase.FAILED
    assert (await sessions.get_session(session_id)).status == LifecycleStatus.CLOSING
    assert not await sessions.is_runner_running(session_id)
    assert (
        await sessions.start_runner(session_id, activity_id=key, user_prompt="original") == terminal
    )
    # Terminal confirmation cannot create a new native record even though none exists.
    start.assert_awaited_once()

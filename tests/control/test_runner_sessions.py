"""Real native execution queries and Worker-only business admission."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai import Agent
from pydantic_ai.capabilities import Hooks
from pydantic_ai.models.test import TestModel as DeterministicModel
from sqlalchemy import text
from temporalio import activity
from temporalio.client import ActivityFailureError, ActivityHandle
from temporalio.exceptions import ApplicationError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker import Worker

from kapy.control.models import ModelService
from kapy.control.sessions import (
    ActivityIdentityConflict,
    CreateSession,
    SessionBusy,
    SessionExecutionError,
    SessionService,
    UpdateSession,
)
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.lifecycle import LifecycleStatus
from kapy.runner import RunnerActivity, RunnerDeps, activities

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def queued(database, runtime):
    return SessionService(
        database.sessions,
        temporal_client=runtime.client,
        temporal_task_queue=f"unpolled-{uuid4()}",
    )


@pytest.fixture
def observed_run(monkeypatch):
    """Hold a real Agent's deterministic model stream after its admission and hooks."""
    started, release = asyncio.Event(), asyncio.Event()
    calls, hooks = [], []
    request_stream = DeterministicModel.request_stream

    @asynccontextmanager
    async def stream(self, messages, model_settings, parameters, run_context=None):
        info = activity.info()
        calls.append((UUID(info.activity_id), info.activity_run_id, dict(model_settings or {})))
        started.set()
        await release.wait()
        async with request_stream(
            self, messages, model_settings, parameters, run_context=run_context
        ) as response:
            yield response

    @asynccontextmanager
    async def model(*args, **kwargs):
        yield DeterministicModel(custom_output_text="done")

    async def before_run(ctx):
        hooks.append((ctx.deps.activity_id, ctx.deps.activity_run_id))

    monkeypatch.setattr(DeterministicModel, "request_stream", stream)
    monkeypatch.setattr(activities, "build_model", model)
    monkeypatch.setattr(
        activities,
        "agent",
        Agent(
            name=activities.agent.name,
            deps_type=RunnerDeps,
            capabilities=[
                *activities.agent.root_capability.capabilities,
                Hooks(before_run=before_run),
            ],
        ),
    )
    return started, release, calls, hooks


async def test_competing_workers_admit_one_idle_session_without_loser_side_effects(
    database, seed_session, session_runtime, monkeypatch, observed_run
):
    session_id = uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    handles = await asyncio.gather(
        *(
            service.start_runner(session_id, activity_id=uuid4(), user_prompt="input")
            for _ in range(2)
        )
    )
    arrived, release_claims = asyncio.Event(), asyncio.Event()
    contenders = []
    claim_run = SessionRepository.claim_run

    async def claim(repo, target, activity_id, run_id):
        contenders.append((activity_id, run_id))
        if len(contenders) == 2:
            arrived.set()
        await release_claims.wait()
        return await claim_run(repo, target, activity_id, run_id)

    monkeypatch.setattr(SessionRepository, "claim_run", claim)
    started, release_model, calls, hooks = observed_run
    runner = RunnerActivity(database.sessions, session_runtime.outputs, session_runtime.client)
    async with Worker(
        session_runtime.client,
        task_queue=(await handles[0].describe()).task_queue,
        activities=[runner.run],
    ):
        results = [asyncio.create_task(handle.result()) for handle in handles]
        try:
            await asyncio.wait_for(arrived.wait(), 10)
            assert set(contenders) == {(UUID(handle.id), handle.run_id) for handle in handles}
            row = await service.get_session(session_id)
            assert row.current_activity_id is None and row.current_activity_run_id is None
            release_claims.set()
            await asyncio.wait_for(started.wait(), 10)
            done, pending = await asyncio.wait(
                results, timeout=10, return_when=asyncio.FIRST_COMPLETED
            )
            assert len(done) == len(pending) == 1
            failure = next(iter(done)).exception()
            assert isinstance(failure, ActivityFailureError)
            assert (
                isinstance(failure.cause, ApplicationError) and failure.cause.type == "SessionBusy"
            )
            winner = handles[results.index(next(iter(pending)))]
            identity = UUID(winner.id), winner.run_id
            assert calls == [(*identity, {})] and hooks == [identity]
            row = await service.get_session(session_id)
            assert (row.current_activity_id, row.current_activity_run_id) == identity
            assert row.status == LifecycleStatus.READY and row.model_settings == {}
            assert (await service.read_history(session_id)).items == []
            async with database.sessions.begin() as db:
                stored = await db.get(SessionRow, session_id)
                assert stored.runner_state is None and stored.layer_states == {}
            release_model.set()
            assert await asyncio.wait_for(next(iter(pending)), 10) == "done"
            row = await service.get_session(session_id)
            assert row.current_activity_id is None and row.current_activity_run_id is None
            assert row.status == LifecycleStatus.READY
            assert calls == [(*identity, {})] and hooks == [identity]
        finally:
            release_claims.set()
            release_model.set()
            await asyncio.wait_for(asyncio.gather(*results, return_exceptions=True), 10)


@pytest.mark.parametrize("first", ["edit", "run"])
async def test_configuration_edit_and_worker_admission_share_one_atomic_boundary(
    database, seed_session, session_runtime, monkeypatch, observed_run, wait_for_lock, first
):
    session_id = uuid4()
    await seed_session(session_id)
    service = session_runtime.service(database.sessions)
    entered, release_first = asyncio.Event(), asyncio.Event()
    contender_pid = asyncio.get_running_loop().create_future()
    claim_run, update_session = SessionRepository.claim_run, SessionRepository.update_session

    async def claim(repo, *args):
        if first == "edit":
            contender_pid.set_result(
                (await repo._db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
        row = await claim_run(repo, *args)
        if first == "run":
            entered.set()
            await release_first.wait()
        return row

    async def edit(repo, *args):
        if first == "run":
            contender_pid.set_result(
                (await repo._db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
        row = await update_session(repo, *args)
        if first == "edit":
            entered.set()
            await release_first.wait()
        return row

    monkeypatch.setattr(SessionRepository, "claim_run", claim)
    monkeypatch.setattr(SessionRepository, "update_session", edit)
    started, release_model, calls, hooks = observed_run
    edit_task = result = None
    handle = None
    settings = {"temperature": 0.75}
    try:
        if first == "edit":
            edit_task = asyncio.create_task(
                service.update_session(session_id, UpdateSession(model_settings=settings))
            )
        else:
            handle = await service.start_runner(
                session_id, activity_id=uuid4(), user_prompt="input"
            )
        await asyncio.wait_for(entered.wait(), 10)
        if first == "edit":
            handle = await service.start_runner(
                session_id, activity_id=uuid4(), user_prompt="input"
            )
        else:
            edit_task = asyncio.create_task(
                service.update_session(session_id, UpdateSession(model_settings=settings))
            )
        assert handle is not None and edit_task is not None
        result = asyncio.create_task(handle.result())
        await wait_for_lock(await asyncio.wait_for(contender_pid, 10))
        release_first.set()
        await asyncio.wait_for(started.wait(), 10)
        if first == "edit":
            assert (await edit_task).model_settings == settings
        else:
            with pytest.raises(SessionBusy):
                await edit_task
        expected_settings = settings if first == "edit" else {}
        identity = UUID(handle.id), handle.run_id
        assert calls == [(*identity, expected_settings)] and hooks == [identity]
        row = await service.get_session(session_id)
        assert (row.current_activity_id, row.current_activity_run_id) == identity
        assert row.model_settings == expected_settings
        release_model.set()
        assert await asyncio.wait_for(result, 10) == "done"
        row = await service.get_session(session_id)
        assert row.current_activity_id is None and row.current_activity_run_id is None
    finally:
        release_first.set()
        release_model.set()
        await asyncio.wait_for(
            asyncio.gather(
                *(task for task in (edit_task, result) if task is not None), return_exceptions=True
            ),
            10,
        )


async def test_queue_has_no_database_owner_and_first_native_input_wins(
    database,
    seed_session,
    session_runtime,
):
    session_id, key = uuid4(), uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    first = await service.start_runner(session_id, activity_id=key, user_prompt="first")
    second = await service.start_runner(session_id, activity_id=key, user_prompt="changed")
    another = await service.start_runner(session_id, activity_id=uuid4(), user_prompt="queued too")
    try:
        assert (
            isinstance(first, ActivityHandle)
            and first.id == second.id
            and first.run_id == second.run_id
        )
        row = await service.update_session(session_id, UpdateSession(title="queue permits edits"))
        assert row.status == LifecycleStatus.READY and row.current_activity_id is None
        facts = await service.get_activity(session_id, key)
        assert facts.status == "RUNNING" and facts.run_state == "SCHEDULED"
        assert facts.run_id == first.run_id and facts.error is None
        description = await first.describe(include_input=True)
        inputs = await description.input()
        assert inputs is not None
        (request,) = inputs
        assert request == {"session_id": str(session_id), "user_prompt": "first"}
        with pytest.raises(ActivityIdentityConflict):
            await service.start_runner(uuid4(), activity_id=key, user_prompt="other session")
        # Reads and explicit close do not interpret a queued Activity as occupancy.
        closed = await session_runtime.service(database.sessions).close_session(
            session_id, activity_id=uuid4()
        )
        assert closed.status == LifecycleStatus.CLOSED
    finally:
        await first.terminate(reason="test cleanup")
        await another.terminate(reason="test cleanup")
    terminal = await service.get_activity(session_id, key, run_id=first.run_id)
    assert terminal.status == "TERMINATED" and terminal.error == "Terminated"
    retained = await service.start_runner(session_id, activity_id=key, user_prompt="any prompt")
    assert retained.run_id == first.run_id
    assert (await service.get_session(session_id)).status == LifecycleStatus.CLOSED


async def test_queued_create_is_queryable_without_a_business_row(database, session_runtime):
    key = uuid4()
    service = queued(database, session_runtime)
    task = asyncio.create_task(
        service.create_session(
            CreateSession(provider_id=uuid4(), model_name="test"),
            activity_id=key,
        )
    )
    try:
        async with asyncio.timeout(5):
            while True:
                try:
                    facts = await service.get_activity(key, key)
                    break
                except LookupError:
                    await asyncio.sleep(0.01)
        assert facts.status == "RUNNING" and facts.run_state == "SCHEDULED"
        with pytest.raises(LookupError):
            await service.get_session(key)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await service.get_activity(key, key)).status == "RUNNING"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await session_runtime.client.get_activity_handle(str(key), run_id=facts.run_id).terminate(
            reason="cleanup"
        )


@pytest.mark.parametrize("same_key", [False, True])
async def test_concurrent_submissions_are_accepted_without_reserving(
    database, seed_session, session_runtime, same_key
):
    session_id, key = uuid4(), uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    handles = await asyncio.gather(
        *(
            service.start_runner(
                session_id, activity_id=key if same_key else uuid4(), user_prompt="same"
            )
            for _ in range(2)
        )
    )
    try:
        assert len({handle.run_id for handle in handles}) == (1 if same_key else 2)
        assert (await service.get_session(session_id)).current_activity_id is None
    finally:
        for handle in {handle.id: handle for handle in handles}.values():
            await handle.terminate(reason="cleanup")


async def test_http_returns_only_reference_and_native_queries_validate_session(
    database, seed_session, session_runtime
):
    from kapy.interfaces.http import create_router

    session_id, key = uuid4(), uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    app = FastAPI()
    app.include_router(create_router(ModelService(database.sessions), service))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            path = f"/api/sessions/{session_id}/runner"
            assert (await http.post(path, json={"user_prompt": "hi"})).status_code == 422
            body = {"activity_id": str(key), "user_prompt": "private prompt"}
            response = await http.post(path, json=body)
            assert response.status_code == 202
            reference = response.json()
            assert set(reference) == {"activity_id", "run_id"}
            assert (
                await http.post(path, json=body | {"user_prompt": "changed"})
            ).json() == reference
            facts = await http.get(
                f"/api/sessions/{session_id}/activities/{key}",
                params={"run_id": reference["run_id"]},
            )
            assert facts.json()["run_state"] == "SCHEDULED"
            assert "private prompt" not in facts.text and "test-key" not in facts.text
            assert (await http.get(f"/api/sessions/{uuid4()}/activities/{key}")).status_code == 409
            assert (
                await http.get(f"/api/sessions/{session_id}/operations/{key}")
            ).status_code == 404
            assert (await http.get(path)).status_code == 405
    finally:
        await session_runtime.client.get_activity_handle(str(key)).terminate(reason="cleanup")


@pytest.mark.parametrize("status", [None, "TERMINATED"])
async def test_only_explicit_close_can_recover_abandoned_ownership(
    database, seed_session, session_runtime, status
):
    session_id = uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    if status:
        stale = await service.start_runner(session_id, activity_id=uuid4(), user_prompt="unused")
        await stale.terminate(reason="test abandoned writer")
        owner, run_id = UUID(stale.id), stale.run_id
    else:
        owner, run_id = uuid4(), str(uuid4())
    assert run_id is not None
    async with database.sessions.begin() as db:
        await SessionRepository(db).claim_run(session_id, owner, run_id)
    # Queries leave the abandoned writer intact. A normal run fails admission.
    if status:
        await service.get_activity(session_id, owner, run_id=run_id)
    live = session_runtime.service(database.sessions)
    handle = await live.start_runner(
        session_id, activity_id=uuid4(), user_prompt="must not execute"
    )
    from temporalio.client import ActivityFailureError

    with pytest.raises(ActivityFailureError):
        await handle.result()
    assert (await live.get_activity(session_id, UUID(handle.id))).error == "SessionBusy"
    assert (await live.get_session(session_id)).current_activity_run_id == run_id
    assert (
        await live.close_session(session_id, activity_id=uuid4())
    ).status == LifecycleStatus.CLOSED
    assert (await live.get_session(session_id)).current_activity_id is None


async def test_close_does_not_take_over_running_or_unknown_execution(
    database, seed_session, session_runtime, monkeypatch
):
    session_id = uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    handle = await service.start_runner(session_id, activity_id=uuid4(), user_prompt="unused")
    from uuid import UUID

    owner = UUID(handle.id)
    assert handle.run_id is not None
    async with database.sessions.begin() as db:
        await SessionRepository(db).claim_run(session_id, owner, handle.run_id)
    live = session_runtime.service(database.sessions)
    try:
        with pytest.raises(SessionExecutionError) as busy:
            await live.close_session(session_id, activity_id=uuid4())
        assert busy.value.category == "SessionBusy"
        original = session_runtime.client.get_activity_handle
        old = original(handle.id, run_id=handle.run_id)
        monkeypatch.setattr(
            old,
            "describe",
            AsyncMock(side_effect=RPCError("private", RPCStatusCode.UNAVAILABLE, b"")),
        )
        monkeypatch.setattr(
            session_runtime.client,
            "get_activity_handle",
            lambda id, **kw: old if id == handle.id else original(id, **kw),
        )
        with pytest.raises(SessionExecutionError) as unknown:
            await live.close_session(session_id, activity_id=uuid4())
        assert unknown.value.category == "UnableToConfirm"
        assert (await live.get_session(session_id)).current_activity_run_id == handle.run_id
    finally:
        await handle.terminate(reason="cleanup")


async def test_submission_failure_does_not_touch_business_state(
    database, seed_session, session_runtime, monkeypatch
):
    session_id = uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    monkeypatch.setattr(
        session_runtime.client,
        "start_activity",
        AsyncMock(side_effect=RPCError("private", RPCStatusCode.UNAVAILABLE, b"")),
    )
    with pytest.raises(RPCError):
        await service.start_runner(session_id, activity_id=uuid4(), user_prompt="hello")
    row = await service.get_session(session_id)
    assert row.status == LifecycleStatus.READY and row.current_activity_id is None


async def test_lost_confirmation_keeps_known_native_run_identity(
    database, session_runtime, monkeypatch
):
    service = queued(database, session_runtime)
    key = uuid4()
    client = session_runtime.client
    original = client.get_activity_handle
    handles = []

    def unavailable(activity_id, **kwargs):
        handle = original(activity_id, **kwargs)
        handles.append(handle)
        monkeypatch.setattr(
            handle,
            "describe",
            AsyncMock(
                side_effect=RPCError("private", RPCStatusCode.UNAVAILABLE, b""),
            ),
        )
        return handle

    monkeypatch.setattr(client, "get_activity_handle", unavailable)
    with pytest.raises(SessionExecutionError) as failure:
        await service.create_session(
            CreateSession(provider_id=uuid4(), model_name="test"), activity_id=key
        )
    assert failure.value.run_id and failure.value.run_id == handles[0].run_id
    assert failure.value.session_id == key and failure.value.activity_id == key
    fixed = original(str(key), run_id=failure.value.run_id)
    assert (await fixed.describe()).run_state.name == "SCHEDULED"
    await fixed.terminate(reason="cleanup")


async def test_deleting_provider_while_queued_fails_without_business_admission(
    database,
    seed_session,
    session_runtime,
):
    from temporalio.client import ActivityFailureError

    from kapy.control.models.models import ProviderRow

    session_id = uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    handle = await service.start_runner(session_id, activity_id=uuid4(), user_prompt="unused")
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        await db.delete(await db.get(ProviderRow, row.provider_id))
    runner = RunnerActivity(database.sessions, session_runtime.outputs, session_runtime.client)
    async with Worker(
        session_runtime.client,
        task_queue=(await handle.describe()).task_queue,
        activities=[runner.run],
    ):
        with pytest.raises(ActivityFailureError):
            await handle.result()
    facts = await service.get_activity(session_id, UUID(handle.id), run_id=handle.run_id)
    assert facts.error == "NotFound"
    row = await service.get_session(session_id)
    assert row.status == LifecycleStatus.READY and row.current_activity_id is None

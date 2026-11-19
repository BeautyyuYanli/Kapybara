"""Real native execution queries and Worker-only business admission."""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, sentinel
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
    SessionAlreadyExists,
    SessionBusy,
    SessionRecord,
    SessionService,
    UpdateSession,
)
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.lifecycle import LifecycleError, LifecycleStatus
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

    async def claim(repo, target, activity_id, run_id, *, previous=None):
        contenders.append((activity_id, run_id))
        if len(contenders) == 2:
            arrived.set()
        await release_claims.wait()
        return await claim_run(repo, target, activity_id, run_id, previous=previous)

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

    async def claim(repo, *args, **kwargs):
        if first == "edit":
            contender_pid.set_result(
                (await repo._db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
        row = await claim_run(repo, *args, **kwargs)
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
        closed = await session_runtime.service(database.sessions).close_session(session_id)
        assert closed.status == LifecycleStatus.CLOSED
    finally:
        await first.terminate(reason="test cleanup")
        await another.terminate(reason="test cleanup")
    terminal = await service.get_activity(session_id, key, run_id=first.run_id)
    assert terminal.status == "TERMINATED" and terminal.error == "Terminated"
    with pytest.raises(LifecycleError):
        await service.start_runner(session_id, activity_id=key, user_prompt="any prompt")
    assert (await service.get_session(session_id)).status == LifecycleStatus.CLOSED


async def test_create_and_close_are_local_and_keep_a_permanent_identity(database):
    service = SessionService(database.sessions)
    request = CreateSession(
        id=uuid4(),
        provider_id=uuid4(),
        model_name="unconfigured",
        layers={"shell": {"cwd": "/remote"}},
    )
    created, duplicate = await asyncio.gather(
        service.create_session(request), service.create_session(request), return_exceptions=True
    )
    if isinstance(created, SessionAlreadyExists):
        created, duplicate = duplicate, created
    assert isinstance(created, SessionRecord)
    assert isinstance(duplicate, SessionAlreadyExists)
    assert duplicate.session_id == created.id == request.id
    assert created.status == LifecycleStatus.READY
    assert created.current_activity_id is created.current_activity_run_id is None
    closed = await service.close_session(created.id)
    assert closed.status == LifecycleStatus.CLOSED and closed.layers == created.layers
    assert await service.close_session(created.id) == closed
    assert (await service.list_sessions()).items == [closed]
    with pytest.raises(SessionAlreadyExists):
        await service.create_session(request)
    with pytest.raises(LifecycleError):
        await service.update_session(created.id, UpdateSession(title="closed session"))
    with pytest.raises(LifecycleError):
        await service.start_runner(created.id, activity_id=uuid4())
    with pytest.raises(LookupError):
        await service.close_session(uuid4())


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

    session_id, key, prompt_key = uuid4(), uuid4(), uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    app = FastAPI()
    app.include_router(create_router(ModelService(database.sessions), service))
    handles: list[ActivityHandle[str]] = []
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://test"
        ) as http:
            path = f"/api/sessions/{session_id}/runner"
            assert (await http.post(path, json={"user_prompt": "hi"})).status_code == 422
            body = {"activity_id": str(key)}
            response = await http.post(path, json=body)
            assert response.status_code == 202
            reference = response.json()
            assert set(reference) == {"activity_id", "run_id"}
            assert (await http.post(path, json=body | {"user_prompt": None})).json() == reference
            handle = session_runtime.client.get_activity_handle(
                str(key), run_id=reference["run_id"], result_type=str
            )
            handles.append(handle)
            description = await handle.describe(include_input=True)
            assert await description.input() == [
                {"session_id": str(session_id), "user_prompt": None}
            ]

            prompt = "private prompt"
            response = await http.post(
                path, json={"activity_id": str(prompt_key), "user_prompt": prompt}
            )
            assert response.status_code == 202
            supplied = response.json()
            handle = session_runtime.client.get_activity_handle(
                str(prompt_key), run_id=supplied["run_id"], result_type=str
            )
            handles.append(handle)
            description = await handle.describe(include_input=True)
            assert await description.input() == [
                {"session_id": str(session_id), "user_prompt": prompt}
            ]
            facts = await http.get(
                f"/api/sessions/{session_id}/activities/{prompt_key}",
                params={"run_id": supplied["run_id"]},
            )
            assert facts.json()["run_state"] == "SCHEDULED"
            assert prompt not in facts.text and "test-key" not in facts.text
            assert (await http.get(f"/api/sessions/{uuid4()}/activities/{key}")).status_code == 409
            assert (
                await http.get(f"/api/sessions/{session_id}/operations/{key}")
            ).status_code == 404
            assert (await http.get(path)).status_code == 405
    finally:
        for handle in handles:
            await handle.terminate(reason="cleanup")


@pytest.mark.parametrize("status", [None, "TERMINATED"])
@pytest.mark.parametrize("operation", ["run", "close"])
async def test_run_or_close_recovers_abandoned_ownership(
    database, seed_session, session_runtime, status, operation, observed_run
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
    # Queries leave the abandoned writer intact.
    if status:
        await service.get_activity(session_id, owner, run_id=run_id)
    live = session_runtime.service(database.sessions)
    assert (await live.get_session(session_id)).current_activity_run_id == run_id
    _, release, calls, _ = observed_run
    release.set()
    if operation == "run":
        handle = await live.start_runner(session_id, activity_id=uuid4(), user_prompt="take over")
        assert await asyncio.wait_for(handle.result(), 10) == "done"
        assert calls == [(UUID(handle.id), handle.run_id, {})]
        expected = LifecycleStatus.READY
    else:
        await live.close_session(session_id)
        assert not calls
        expected = LifecycleStatus.CLOSED
    record = await live.get_session(session_id)
    assert record.status == expected
    assert record.current_activity_id is record.current_activity_run_id is None


async def test_run_reclaims_an_abandoned_writer_when_the_same_activity_uuid_is_reused(
    database, seed_session, session_runtime, observed_run
):
    from temporalio.api.workflowservice.v1 import DeleteActivityExecutionRequest

    session_id, activity_id = uuid4(), uuid4()
    await seed_session(session_id)
    old = await queued(database, session_runtime).start_runner(
        session_id, activity_id=activity_id, user_prompt="old run"
    )
    await old.terminate(reason="test abandoned writer")
    assert old.run_id is not None
    async with database.sessions.begin() as db:
        assert await SessionRepository(db).claim_run(session_id, activity_id, old.run_id)

    client = session_runtime.client
    await client.workflow_service.delete_activity_execution(
        DeleteActivityExecutionRequest(
            namespace=client.namespace, activity_id=old.id, run_id=old.run_id
        )
    )
    service = session_runtime.service(database.sessions)
    with pytest.raises(LookupError):
        await service.get_activity(session_id, activity_id, run_id=old.run_id)

    _, release, calls, _ = observed_run
    release.set()
    replacement = await service.start_runner(
        session_id, activity_id=activity_id, user_prompt="new run"
    )
    assert replacement.id == old.id and replacement.run_id != old.run_id
    assert await asyncio.wait_for(replacement.result(), 10) == "done"
    assert calls == [(activity_id, replacement.run_id, {})]
    record = await service.get_session(session_id)
    assert record.status == LifecycleStatus.READY
    assert record.current_activity_id is record.current_activity_run_id is None


async def test_close_and_runner_cannot_take_over_running_or_unknown_execution(
    database, seed_session, session_runtime, monkeypatch
):
    session_id = uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    handle = await service.start_runner(session_id, activity_id=uuid4(), user_prompt="unused")
    owner = UUID(handle.id)
    assert handle.run_id is not None
    async with database.sessions.begin() as db:
        await SessionRepository(db).claim_run(session_id, owner, handle.run_id)
    live = session_runtime.service(database.sessions)
    try:
        with pytest.raises(SessionBusy):
            await live.close_session(session_id)
        original = session_runtime.client.get_activity_handle
        old = original(handle.id, run_id=handle.run_id)
        rpc_error = RPCError("private", RPCStatusCode.UNAVAILABLE, b"")
        monkeypatch.setattr(old, "describe", AsyncMock(side_effect=rpc_error))
        monkeypatch.setattr(
            session_runtime.client,
            "get_activity_handle",
            lambda id, **kw: old if id == handle.id else original(id, **kw),
        )
        with pytest.raises(RPCError) as unknown:
            await live.close_session(session_id)
        assert unknown.value is rpc_error
        contender_id = uuid4()
        contender = await live.start_runner(session_id, activity_id=contender_id, user_prompt="run")
        with pytest.raises(ActivityFailureError) as failure:
            await asyncio.wait_for(contender.result(), 10)
        assert isinstance(failure.value.cause, ApplicationError)
        assert failure.value.cause.type == "UnableToConfirm" and failure.value.cause.non_retryable
        assert (await live.get_activity(session_id, contender_id)).error == "UnableToConfirm"
        assert (await live.get_session(session_id)).current_activity_run_id == handle.run_id
    finally:
        await handle.terminate(reason="cleanup")


@pytest.mark.parametrize("changed_to", ["closed", "idle", "new_writer"])
async def test_close_rechecks_a_writer_changed_during_the_temporal_probe(
    database, seed_session, monkeypatch, changed_to
):
    from kapy.control.sessions import service as service_module

    session_id, old_id, new_id = uuid4(), uuid4(), uuid4()
    await seed_session(session_id)
    previous = (old_id, str(uuid4()))
    new_run_id = str(uuid4())
    async with database.sessions.begin() as db:
        assert await SessionRepository(db).claim_run(session_id, *previous) is not None

    async def probe(client, activity_id, run_id):
        assert client is sentinel.temporal_client
        assert (activity_id, run_id) == previous
        async with database.sessions.begin() as db:
            repo = SessionRepository(db)
            if changed_to == "closed":
                assert await repo.close_session(session_id, previous=previous) is not None
            elif changed_to == "idle":
                await repo.complete(session_id, *previous)
            else:
                assert (
                    await repo.claim_run(session_id, new_id, new_run_id, previous=previous)
                    is not None
                )

    monkeypatch.setattr(service_module, "ensure_activity_terminal", probe)
    service = SessionService(database.sessions, temporal_client=sentinel.temporal_client)
    if changed_to == "new_writer":
        with pytest.raises(SessionBusy):
            await asyncio.wait_for(service.close_session(session_id), 5)
        current = await service.get_session(session_id)
        assert (current.current_activity_id, current.current_activity_run_id) == (
            new_id,
            new_run_id,
        )
    else:
        closed = await asyncio.wait_for(service.close_session(session_id), 5)
        assert closed.status == LifecycleStatus.CLOSED
        assert closed.current_activity_id is closed.current_activity_run_id is None


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


async def test_lost_run_confirmation_propagates_rpc_error_and_preserves_submission(
    database, seed_session, session_runtime, monkeypatch
):
    service = queued(database, session_runtime)
    session_id, key = uuid4(), uuid4()
    await seed_session(session_id)
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
    with pytest.raises(RPCError) as failure:
        await service.start_runner(session_id, activity_id=key, user_prompt="kept")
    assert failure.value.status == RPCStatusCode.UNAVAILABLE
    assert handles[0].id == str(key) and handles[0].run_id
    fixed = original(str(key), run_id=handles[0].run_id)
    assert (await fixed.describe()).run_state.name == "SCHEDULED"
    await fixed.terminate(reason="cleanup")


@pytest.mark.parametrize("abandoned", [False, True])
async def test_deleting_provider_while_queued_fails_without_business_admission(
    database,
    seed_session,
    session_runtime,
    abandoned,
):
    from kapy.control.models.models import ProviderRow

    session_id = uuid4()
    await seed_session(session_id)
    service = queued(database, session_runtime)
    handle = await service.start_runner(session_id, activity_id=uuid4(), user_prompt="unused")
    previous = (uuid4(), str(uuid4())) if abandoned else (None, None)
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        assert row is not None
        row.current_activity_id, row.current_activity_run_id = previous
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
    assert row.status == LifecycleStatus.READY
    assert (row.current_activity_id, row.current_activity_run_id) == previous

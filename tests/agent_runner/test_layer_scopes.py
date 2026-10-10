"""Business scope initialization and lease-fenced cleanup use real PostgreSQL.

The test layers model service-owned identities and partial cleanup. SDK component
construction is deliberately unrelated to these session-level hook calls.
"""

import asyncio
from types import ModuleType
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from sqlalchemy.util.concurrency import await_only

from kapy.control.models import ModelService
from kapy.control.sessions import CreateSession, SessionService
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.interfaces.http import create_router
from kapy.layers import RetryableScopeError
from kapy.layers.examples import echo
from kapy.layers.registry import REGISTRY
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.session_lease import LeaseLost
from kapy.session_lease.models import SessionLeaseRow

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def module(*, config=echo.Config, state=echo.State, **hooks):
    result = ModuleType("scope_test_layer")
    result.__dict__.update(
        {"Config": config, "State": state, "Capability": echo.Capability, **hooks}
    )
    return result


def selection(**layers):
    return CreateSession(provider_id=uuid4(), model_name="test", layers=layers)


async def stored(database, session_id):
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        assert row is not None
        return row.layer_states


async def test_enter_initializes_required_state_and_hooks_are_independently_optional(
    database, monkeypatch
):
    class State(BaseModel):
        resource: UUID

    enters, exits = [], []

    async def enter(scope_id, config):
        enters.append((scope_id, config))
        return State(resource=scope_id)

    async def exit_hook(scope_id, config, state):
        exits.append((scope_id, config, state))

    monkeypatch.setitem(
        REGISTRY, "both", module(state=State, enter_scope=enter, exit_scope=exit_hook)
    )
    monkeypatch.setitem(REGISTRY, "enter", module(state=State, enter_scope=enter))
    monkeypatch.setitem(REGISTRY, "exit", module(exit_scope=exit_hook))
    monkeypatch.setitem(REGISTRY, "default", echo)
    service = SessionService(database.sessions)
    session = await service.create_session(
        selection(both={"prefix": "fixed:"}, enter={}, exit={}, default={})
    )
    assert [scope_id for scope_id, _ in enters] == [session.id, session.id]
    assert isinstance(enters[0][1], echo.Config) and enters[0][1].prefix == "fixed:"
    assert await stored(database, session.id) == {
        "both": {"resource": str(session.id)},
        "enter": {"resource": str(session.id)},
        "exit": {"calls": 0},
        "default": {"calls": 0},
    }
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
    assert [scope_id for scope_id, _, _ in exits] == [session.id, session.id]
    assert isinstance(exits[0][2], State) and isinstance(exits[1][2], echo.State)
    await service.close_session(session.id)
    assert len(enters) == len(exits) == 2


async def test_enter_receives_the_same_validated_config_as_persisted(database, monkeypatch):
    class Config(BaseModel):
        items: list[str]

        @model_validator(mode="before")
        @classmethod
        def normalize(cls, value):
            value["items"].append("validated")
            return value

    received = []

    async def enter(scope_id, config):
        received.append(list(config.items))
        config.items.append("hook-local")
        return echo.State()

    monkeypatch.setitem(REGISTRY, "scoped", module(config=Config, enter_scope=enter))
    data = selection(scoped={"items": ["input"]})
    session = await SessionService(database.sessions).create_session(data)
    assert received == [["input", "validated"]]
    assert session.layers == {"scoped": {"items": ["input", "validated"]}}
    assert data.layers == {"scoped": {"items": ["input"]}}


@pytest.mark.parametrize("bad", ["unknown", "config", "export", "sync_hook"])
async def test_every_export_and_config_is_validated_before_any_enter(database, monkeypatch, bad):
    enters = []

    async def enter(scope_id, config):
        enters.append(scope_id)
        return echo.State()

    monkeypatch.setitem(REGISTRY, "first", module(enter_scope=enter))
    second = (
        module(enter_scope=lambda scope_id, config: echo.State()) if bad == "sync_hook" else echo
    )
    if bad == "export":
        second = module(state=dict)
    monkeypatch.setitem(REGISTRY, "second", second)
    layers = {"first": {}, "unknown" if bad == "unknown" else "second": {}}
    if bad == "config":
        layers["second"] = {"unknown": True}
    service = SessionService(database.sessions)
    with pytest.raises((ValueError, ValidationError)):
        await service.create_session(selection(**layers))
    assert not enters and not (await service.list_sessions()).items


@pytest.mark.parametrize("succeeds", [False, True])
async def test_enter_retries_are_bounded_with_fixed_identity_and_fresh_config(
    database, monkeypatch, succeeds
):
    calls = []
    error = RetryableScopeError("temporary allocation failure")

    async def enter(scope_id, config):
        calls.append((scope_id, config.prefix))
        config.prefix = "hook-local mutation"
        if not succeeds or len(calls) < 3:
            raise error
        return echo.State(calls=7)

    monkeypatch.setitem(REGISTRY, "scoped", module(enter_scope=enter))
    service = SessionService(database.sessions)
    if succeeds:
        session = await service.create_session(selection(scoped={"prefix": "fixed"}))
        assert calls == [(session.id, "fixed")] * 3
        assert session.layers["scoped"]["prefix"] == "fixed"
        assert await stored(database, session.id) == {"scoped": {"calls": 7}}
    else:
        with pytest.raises(RetryableScopeError) as raised:
            await service.create_session(selection(scoped={"prefix": "fixed"}))
        assert raised.value is error and len(calls) == 3
        assert calls == [(calls[0][0], "fixed")] * 3
        assert not (await service.list_sessions()).items


@pytest.mark.parametrize(
    "failure", ["ordinary", "cause", "group", "invalid_state", "wrong_type", "cancel"]
)
async def test_enter_only_explicit_outer_retryable_error_allows_retry(
    database, monkeypatch, failure
):
    class State(BaseModel):
        calls: int = Field(default=0, ge=0)

    calls = []

    async def enter(scope_id, config):
        calls.append(scope_id)
        if failure == "cause":
            raise RuntimeError("ordinary outer failure") from RetryableScopeError("inner")
        if failure == "group":
            raise ExceptionGroup("group", [RetryableScopeError("inner")])
        if failure == "invalid_state":
            return State.model_construct(calls=-1)
        if failure == "wrong_type":
            return {"calls": 0}
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise RuntimeError("ordinary failure")

    monkeypatch.setitem(REGISTRY, "scoped", module(state=State, enter_scope=enter))
    service = SessionService(database.sessions)
    expected = asyncio.CancelledError if failure == "cancel" else Exception
    with pytest.raises(expected):
        await service.create_session(selection(scoped={}))
    assert len(calls) == 1 and not (await service.list_sessions()).items


async def test_creation_failure_compensates_in_reverse_and_preserves_original_error(
    database, monkeypatch, caplog
):
    scopes, released = [], []
    original = RuntimeError("later initialization failure")

    async def enter(scope_id, config):
        scopes.append(scope_id)
        return echo.State(calls=1)

    async def exit_a(scope_id, config, state):
        released.append(("a", scope_id, state.calls))

    async def exit_b(scope_id, config, state):
        released.append(("b", scope_id, state.calls))
        raise RuntimeError("compensation failed")

    async def fail(scope_id, config):
        raise original

    monkeypatch.setitem(REGISTRY, "a", module(enter_scope=enter, exit_scope=exit_a))
    monkeypatch.setitem(REGISTRY, "b", module(enter_scope=enter, exit_scope=exit_b))
    monkeypatch.setitem(REGISTRY, "fail", module(enter_scope=fail))
    service = SessionService(database.sessions)
    with pytest.raises(RuntimeError) as raised:
        await service.create_session(selection(a={}, b={}, fail={}))
    assert raised.value is original
    assert len(set(scopes)) == 1 and [name for name, _, _ in released] == ["b", "a"]
    assert all(scope_id == scopes[0] and calls == 1 for _, scope_id, calls in released)
    assert str(scopes[0]) in caplog.text and "layer b" in caplog.text
    assert not (await service.list_sessions()).items


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_cancelled_creation_compensates_completed_enters(database, monkeypatch, cancellation):
    initialized, released = [], []
    waiting = asyncio.Event()
    cancelled = False

    async def enter(scope_id, config):
        initialized.append(scope_id)
        return echo.State(calls=1)

    async def exit_hook(scope_id, config, state):
        await anyio.lowlevel.checkpoint()
        released.append((scope_id, state.calls))

    async def blocked_enter(scope_id, config):
        waiting.set()
        await asyncio.Future()

    monkeypatch.setitem(REGISTRY, "first", module(enter_scope=enter, exit_scope=exit_hook))
    monkeypatch.setitem(REGISTRY, "blocked", module(enter_scope=blocked_enter))
    service = SessionService(database.sessions)

    async def create():
        nonlocal cancelled
        try:
            await service.create_session(selection(first={}, blocked={}))
        except asyncio.CancelledError:
            cancelled = True
            raise

    if cancellation == "asyncio":
        task = asyncio.create_task(create())
        await asyncio.wait_for(waiting.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with anyio.create_task_group() as group:
            group.start_soon(create)
            await asyncio.wait_for(waiting.wait(), 5)
            group.cancel_scope.cancel()
    assert cancelled and released == [(initialized[0], 1)]
    assert len(initialized) == 1 and not (await service.list_sessions()).items


async def test_definite_database_failure_rolls_back_and_compensates(database, monkeypatch):
    effects = []

    async def enter(scope_id, config):
        effects.append(("enter", scope_id))
        return echo.State()

    async def exit_hook(scope_id, config, state):
        effects.append(("exit", scope_id))

    original = SessionRepository.create_session

    async def fail(db_repo, *args, **kwargs):
        await original(db_repo, *args, **kwargs)
        raise RuntimeError("definite write failure")

    monkeypatch.setitem(REGISTRY, "scoped", module(enter_scope=enter, exit_scope=exit_hook))
    monkeypatch.setattr(SessionRepository, "create_session", fail)
    service = SessionService(database.sessions)
    with pytest.raises(RuntimeError, match="definite write failure"):
        await service.create_session(selection(scoped={}))
    assert effects == [("enter", effects[0][1]), ("exit", effects[0][1])]
    assert not (await service.list_sessions()).items


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
async def test_uncertain_commit_does_not_compensate_or_probe(
    database, monkeypatch, committed, cancel
):
    effects = []

    async def enter(scope_id, config):
        effects.append(("enter", scope_id))
        return echo.State()

    async def exit_hook(scope_id, config, state):
        effects.append(("exit", scope_id))

    class UncertainCommit(AsyncSession):
        async def commit(self) -> None:
            if committed:
                await super().commit()
            if cancel:
                raise asyncio.CancelledError()
            raise RuntimeError("uncertain commit")

    monkeypatch.setitem(REGISTRY, "scoped", module(enter_scope=enter, exit_scope=exit_hook))
    service = SessionService(
        async_sessionmaker(database.engine, class_=UncertainCommit, expire_on_commit=False)
    )
    expected = asyncio.CancelledError if cancel else RuntimeError
    with pytest.raises(expected):
        await service.create_session(selection(scoped={}))
    assert effects == [("enter", effects[0][1])]
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, effects[0][1])
        assert (row is not None) is committed


async def test_exit_retry_saves_partial_state_before_retry_and_does_not_change_config(
    database, monkeypatch
):
    calls = []

    async def exit_hook(scope_id, config, state):
        assert await stored(database, scope_id) == {"scoped": {"calls": state.calls}}
        calls.append((scope_id, config.prefix, state.calls))
        config.prefix = "exit-local mutation"
        state.calls += 1
        if len(calls) < 3:
            raise RetryableScopeError("partially cleaned")

    monkeypatch.setitem(REGISTRY, "scoped", module(exit_scope=exit_hook))
    service = SessionService(database.sessions)
    session = await service.create_session(selection(scoped={"prefix": "fixed"}))
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
    assert calls == [(session.id, "fixed", value) for value in range(3)]
    assert await stored(database, session.id) == {"scoped": {"calls": 3}}


@pytest.mark.parametrize("retryable", [False, True])
async def test_failed_exit_retains_progress_and_explicit_close_repeats_idempotent_exits(
    database, monkeypatch, retryable
):
    completed, failures = [], []
    failing = True
    error = RetryableScopeError("temporary") if retryable else RuntimeError("ordinary")

    async def exit_a(scope_id, config, state):
        completed.append(scope_id)
        state.calls = 1

    async def exit_b(scope_id, config, state):
        failures.append(scope_id)
        state.calls += 1
        if failing:
            raise error

    monkeypatch.setitem(REGISTRY, "a", module(exit_scope=exit_a))
    monkeypatch.setitem(REGISTRY, "b", module(exit_scope=exit_b))
    service = SessionService(database.sessions)
    session = await service.create_session(selection(a={}, b={}))
    with pytest.raises(type(error)) as raised:
        await service.close_session(session.id)
    assert raised.value is error
    attempts = 3 if retryable else 1
    assert failures == [session.id] * attempts
    assert await stored(database, session.id) == {"a": {"calls": 1}, "b": {"calls": attempts}}
    assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING
    assert not await service.is_runner_running(session.id)
    with pytest.raises(LifecycleError):
        await service.enqueue_input(session.id, "queued", "blocked")
    failing = False
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
    assert completed == [session.id, session.id]
    assert (await stored(database, session.id))["b"] == {"calls": attempts + 1}
    await service.close_session(session.id)
    assert completed == [session.id, session.id]


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_cancelled_exit_saves_mutations_without_retry(database, monkeypatch, cancellation):
    started = asyncio.Event()
    calls = []
    cancelled = False

    async def exit_hook(scope_id, config, state):
        calls.append(scope_id)
        if state.calls == 0:
            state.calls = 1
            started.set()
            await asyncio.Future()

    monkeypatch.setitem(REGISTRY, "scoped", module(exit_scope=exit_hook))
    service = SessionService(database.sessions)
    session = await service.create_session(selection(scoped={}))

    async def close():
        nonlocal cancelled
        try:
            await service.close_session(session.id)
        except asyncio.CancelledError:
            cancelled = True
            raise

    if cancellation == "asyncio":
        task = asyncio.create_task(close())
        await asyncio.wait_for(started.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with anyio.create_task_group() as group:
            group.start_soon(close)
            await asyncio.wait_for(started.wait(), 5)
            group.cancel_scope.cancel()
    assert cancelled and calls == [session.id]
    assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING
    assert await stored(database, session.id) == {"scoped": {"calls": 1}}
    assert not await service.is_runner_running(session.id)
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
    assert calls == [session.id, session.id]


async def test_invalid_exit_mutation_is_not_saved_or_retried(database, monkeypatch):
    class State(BaseModel):
        remaining: int = Field(default=1, ge=0)

    calls = []

    async def exit_hook(scope_id, config, state):
        calls.append(scope_id)
        state.remaining = -1
        raise RetryableScopeError("invalid progress")

    monkeypatch.setitem(REGISTRY, "scoped", module(state=State, exit_scope=exit_hook))
    service = SessionService(database.sessions)
    session = await service.create_session(selection(scoped={}))
    with pytest.raises(ValidationError):
        await service.close_session(session.id)
    assert calls == [session.id]
    assert await stored(database, session.id) == {"scoped": {"remaining": 1}}
    assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING


async def test_lost_lease_fences_exit_progress_and_stops_retry(database, monkeypatch):
    calls = []

    async def exit_hook(scope_id, config, state):
        calls.append(scope_id)
        state.calls = 1
        async with database.sessions.begin() as db:
            row = await db.get(SessionLeaseRow, scope_id)
            assert row is not None
            row.lock_token = uuid4()
        raise RetryableScopeError("partial cleanup")

    monkeypatch.setitem(REGISTRY, "scoped", module(exit_scope=exit_hook))
    service = SessionService(database.sessions)
    session = await service.create_session(selection(scoped={}))
    with pytest.raises(LeaseLost):
        await service.close_session(session.id)
    assert calls == [session.id]
    assert await stored(database, session.id) == {"scoped": {"calls": 0}}
    assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING


@pytest.mark.parametrize("storage_error", [RuntimeError, RetryableScopeError])
async def test_cleanup_storage_failure_stops_hook_retry(database, monkeypatch, storage_error):
    calls = []

    async def exit_hook(scope_id, config, state):
        calls.append(scope_id)
        state.calls = 1
        raise RetryableScopeError("partial cleanup")

    async def unavailable(*args, **kwargs):
        raise storage_error("cleanup storage unavailable")

    monkeypatch.setitem(REGISTRY, "scoped", module(exit_scope=exit_hook))
    monkeypatch.setattr(SessionRepository, "save_layer_cleanup", unavailable)
    service = SessionService(database.sessions)
    session = await service.create_session(selection(scoped={}))
    with pytest.raises(RuntimeError, match="cleanup storage unavailable"):
        await service.close_session(session.id)
    assert calls == [session.id]
    assert await stored(database, session.id) == {"scoped": {"calls": 0}}


@pytest.mark.parametrize("outcome", ["timeout", "cancel"])
async def test_scope_progress_transaction_finishes_before_lease_release_on_abort(
    database, monkeypatch, outcome
):
    committing, finish, commit_exited = asyncio.Event(), asyncio.Event(), asyncio.Event()
    close_finished = asyncio.Event()
    commit_tasks = []
    original = SessionRepository.save_layer_cleanup

    async def mark_progress(repo, *args, **kwargs):
        await original(repo, *args, **kwargs)
        repo._db.info["scope_progress"] = True

    def pause_progress_commit(session):
        if session.info.get("scope_progress"):
            commit_tasks.append(asyncio.current_task())
            committing.set()
            try:
                await_only(finish.wait())
            finally:
                commit_exited.set()

    class ObservedSession(AsyncSession):
        async def close(self) -> None:
            progress = self.info.get("scope_progress")
            await super().close()
            if progress:
                assert self.get_transaction() is None
                close_finished.set()

    async def exit_hook(scope_id, config, state):
        state.calls = 1

    monkeypatch.setitem(REGISTRY, "scoped", module(exit_scope=exit_hook))
    monkeypatch.setattr(SessionRepository, "save_layer_cleanup", mark_progress)
    service = SessionService(
        async_sessionmaker(database.engine, class_=ObservedSession, expire_on_commit=False)
    )
    session = await service.create_session(selection(scoped={}))
    event.listen(Session, "before_commit", pause_progress_commit)
    task = asyncio.create_task(service.close_session(session.id))
    try:
        await asyncio.wait_for(committing.wait(), 5)
        if outcome == "cancel":
            task.cancel()
        expected = asyncio.CancelledError if outcome == "cancel" else TimeoutError
        with pytest.raises(expected):
            await asyncio.wait_for(task, 8)
        # The aborted COMMIT and rollback/close must be finished when the operation
        # returns and releases its lease; storage may not continue afterward.
        assert commit_exited.is_set()
        assert close_finished.is_set()
        assert not await service.is_runner_running(session.id)
        assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING
        assert await stored(database, session.id) == {"scoped": {"calls": 0}}
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
        for pending in commit_tasks:
            if pending is not None and pending is not task and not pending.done():
                await asyncio.wait_for(asyncio.shield(pending), 5)
        event.remove(Session, "before_commit", pause_progress_commit)


@pytest.mark.parametrize("retryable", [False, True])
async def test_http_exposes_only_explicit_retryable_scope_errors_as_503(
    database, monkeypatch, retryable
):
    calls = []

    async def enter(scope_id, config):
        calls.append(scope_id)
        error = RetryableScopeError if retryable else RuntimeError
        raise error("secret upstream details")

    monkeypatch.setitem(REGISTRY, "scoped", module(enter_scope=enter))
    service = SessionService(database.sessions)
    app = FastAPI()
    app.include_router(create_router(ModelService(database.sessions), service))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/sessions", json=selection(scoped={}).model_dump(mode="json")
        )
    assert response.status_code == (503 if retryable else 500)
    assert len(calls) == (3 if retryable else 1)
    assert "secret" not in response.text

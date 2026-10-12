"""Business scope initialization and lease-fenced cleanup use real PostgreSQL.

The test layers model service-owned identities and partial cleanup. SDK component
construction is deliberately unrelated to these session-level hook calls.
"""

import asyncio
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field, ValidationError, model_validator
from pydantic_ai.capabilities import Capability
from sqlalchemy import event
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session
from sqlalchemy.util.concurrency import await_only

from kapy.control.models import ModelService
from kapy.control.sessions import (
    CreateSession,
    SessionAlreadyExists,
    SessionCreationError,
    SessionService,
    UpdateSession,
)
from kapy.control.sessions import service as sessions_service
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.interfaces.http import create_router
from kapy.layers.registry import get_layers
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.session_lease import LeaseLost, SessionBusy
from kapy.session_lease.models import SessionLeaseRow
from pydantic_ai_layer_protocol import Layer, LayerRegistry, RetryableScopeError
from pydantic_ai_layer_protocol.examples import echo

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def layer(layer_id: str, *, config=echo.Config, state=echo.State, **hooks):
    return Layer(
        config_type=config,
        state_type=state,
        capability=Capability(id=layer_id),
        enter_scope=hooks.get("enter_scope"),
        exit_scope=hooks.get("exit_scope"),
    )


def install_layer(monkeypatch, layer_id, definition):
    # Static installations are immutable; each change creates a fresh registry.
    definitions = dict(sessions_service.layer_registry.definitions)
    definitions[layer_id] = definition
    factories = {
        name: (lambda layer_id, access, definition=definition: definition)
        for name, definition in definitions.items()
    }
    monkeypatch.setattr(
        sessions_service, "layer_registry", LayerRegistry(factories, get_layers=get_layers)
    )


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

    install_layer(
        monkeypatch, "both", layer("both", state=State, enter_scope=enter, exit_scope=exit_hook)
    )
    install_layer(monkeypatch, "enter", layer("enter", state=State, enter_scope=enter))
    install_layer(monkeypatch, "exit", layer("exit", exit_scope=exit_hook))
    install_layer(monkeypatch, "default", layer("default"))
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
    assert isinstance(exits[0][2], echo.State) and isinstance(exits[1][2], State)
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

    install_layer(monkeypatch, "scoped", layer("scoped", config=Config, enter_scope=enter))
    data = selection(scoped={"items": ["input"]})
    session = await SessionService(database.sessions).create_session(data)
    assert received == [["input", "validated"]]
    assert session.layers == {"scoped": {"items": ["input", "validated"]}}
    assert data.layers == {"scoped": {"items": ["input"]}}


@pytest.mark.parametrize("bad", ["unknown", "config", "default_state"])
async def test_all_configs_and_default_states_are_validated_before_any_enter(
    database, monkeypatch, bad
):
    enters = []

    class RequiredState(BaseModel):
        resource: str

    async def enter(scope_id, config):
        enters.append(scope_id)
        return echo.State()

    install_layer(monkeypatch, "first", layer("first", enter_scope=enter))
    install_layer(
        monkeypatch,
        "second",
        layer("second", state=RequiredState if bad == "default_state" else echo.State),
    )
    configs = {"first": {}, "unknown" if bad == "unknown" else "second": {}}
    if bad == "config":
        configs["second"] = {"unknown": True}
    service = SessionService(database.sessions)
    with pytest.raises((ValueError, ValidationError)):
        await service.create_session(selection(**configs))
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

    install_layer(monkeypatch, "scoped", layer("scoped", enter_scope=enter))
    service = SessionService(database.sessions)
    if succeeds:
        session = await service.create_session(selection(scoped={"prefix": "fixed"}))
        assert calls == [(session.id, "fixed")] * 3
        assert session.layers["scoped"]["prefix"] == "fixed"
        assert await stored(database, session.id) == {"scoped": {"calls": 7}}
    else:
        with pytest.raises(SessionCreationError) as raised:
            await service.create_session(selection(scoped={"prefix": "fixed"}))
        assert raised.value.__cause__ is error and len(calls) == 3
        assert calls == [(calls[0][0], "fixed")] * 3
        assert (await service.get_session(raised.value.session_id)).status == LifecycleStatus.CLOSED


@pytest.mark.parametrize(
    "failure", ["ordinary", "cause", "group", "invalid_state", "wrong_type", "busy", "cancel"]
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
        if failure == "busy":
            raise SessionBusy("Business hook failed; this is not duplicate creation")
        raise RuntimeError("ordinary failure")

    install_layer(monkeypatch, "scoped", layer("scoped", state=State, enter_scope=enter))
    service = SessionService(database.sessions)
    expected = asyncio.CancelledError if failure == "cancel" else SessionCreationError
    with pytest.raises(expected):
        await service.create_session(selection(scoped={}))
    assert len(calls) == 1
    assert (await service.get_session(calls[0])).status == LifecycleStatus.CLOSED


@pytest.mark.parametrize("error_type", [SessionAlreadyExists, SessionCreationError])
async def test_enter_host_errors_keep_the_current_creation_identity_at_service_and_http(
    database, monkeypatch, error_type
):
    foreign_id = uuid4()
    original = error_type(foreign_id)
    calls = []

    async def enter(scope_id, config):
        calls.append(scope_id)
        raise original

    install_layer(monkeypatch, "scoped", layer("scoped", enter_scope=enter))
    service = SessionService(database.sessions)
    session_id = uuid4()
    with pytest.raises(SessionCreationError) as raised:
        await service.create_session(selection(scoped={}).model_copy(update={"id": session_id}))
    assert raised.value.session_id == session_id
    assert raised.value.__cause__ is original
    assert (await service.get_session(session_id)).status == LifecycleStatus.CLOSED

    app = FastAPI()
    app.include_router(create_router(ModelService(database.sessions), service))
    http_session_id = uuid4()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/sessions",
            json=selection(scoped={}).model_dump(mode="json") | {"id": str(http_session_id)},
        )
    assert response.status_code == 500
    assert response.json()["session_id"] == str(http_session_id)
    assert str(foreign_id) not in response.text
    assert calls == [session_id, http_session_id]
    assert (await service.get_session(http_session_id)).status == LifecycleStatus.CLOSED


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
        released.append((scope_id, state))

    async def blocked_enter(scope_id, config):
        waiting.set()
        await asyncio.Future()

    install_layer(monkeypatch, "first", layer("first", enter_scope=enter, exit_scope=exit_hook))
    install_layer(monkeypatch, "blocked", layer("blocked", enter_scope=blocked_enter))
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
    assert cancelled and released == [(initialized[0], None)]
    assert len(initialized) == 1
    assert (await service.get_session(initialized[0])).status == LifecycleStatus.CLOSED


async def test_registration_failure_never_enters_business_scopes(database, monkeypatch):
    effects = []

    async def enter(scope_id, config):
        effects.append(("enter", scope_id))
        return echo.State()

    async def exit_hook(scope_id, config, state):
        effects.append(("exit", scope_id))

    original = SessionRepository.register_creating

    async def fail(db_repo, *args, **kwargs):
        await original(db_repo, *args, **kwargs)
        raise RuntimeError("definite write failure")

    install_layer(monkeypatch, "scoped", layer("scoped", enter_scope=enter, exit_scope=exit_hook))
    monkeypatch.setattr(SessionRepository, "register_creating", fail)
    service = SessionService(database.sessions)
    with pytest.raises(SessionCreationError) as raised:
        await service.create_session(selection(scoped={}))
    assert str(raised.value.__cause__) == "definite write failure"
    assert effects == []
    assert not (await service.list_sessions()).items


@pytest.mark.parametrize("committed", [False, True])
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("phase", ["register_creating", "publish_ready"])
async def test_uncertain_creation_commit_is_resolved_before_compensation(
    database, monkeypatch, committed, cancel, phase
):
    effects: list[tuple[str, UUID] | tuple[str, UUID, BaseModel | None]] = []

    async def enter(scope_id, config):
        effects.append(("enter", scope_id))
        return echo.State()

    async def exit_hook(scope_id, config, state):
        effects.append(("exit", scope_id, state))

    original = getattr(SessionRepository, phase)

    async def mark(repo, *args, **kwargs):
        result = await original(repo, *args, **kwargs)
        repo._db.info["uncertain_commit"] = True
        return result

    class UncertainCommit(AsyncSession):
        async def commit(self) -> None:
            if not self.info.get("uncertain_commit"):
                await super().commit()
                return
            if committed:
                await super().commit()
            if cancel:
                raise asyncio.CancelledError()
            raise RuntimeError("uncertain commit")

    install_layer(monkeypatch, "scoped", layer("scoped", enter_scope=enter, exit_scope=exit_hook))
    monkeypatch.setattr(SessionRepository, phase, mark)
    service = SessionService(
        async_sessionmaker(database.engine, class_=UncertainCommit, expire_on_commit=False)
    )
    data = selection(scoped={}).model_copy(update={"id": uuid4()})
    if phase == "publish_ready" and committed and not cancel:
        assert (await service.create_session(data)).status == LifecycleStatus.READY
    else:
        expected = asyncio.CancelledError if cancel else SessionCreationError
        with pytest.raises(expected):
            await service.create_session(data)
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, data.id)
        if phase == "register_creating":
            assert effects == []
            assert (row is not None) is committed
            if row is not None:
                assert row.status == LifecycleStatus.CREATING and row.layer_states == {}
        else:
            assert row is not None
            assert effects[0] == ("enter", data.id)
            if committed:
                assert row.status == LifecycleStatus.READY and len(effects) == 1
            else:
                assert row.status == LifecycleStatus.CLOSED
                assert effects == [("enter", data.id), ("exit", data.id, None)]
                assert row.layer_states == {}


async def test_ready_commit_and_resolution_outage_preserves_creating_without_exit(
    database, monkeypatch
):
    resources, exits = [], []
    unavailable = False
    original = SessionRepository.publish_ready

    async def enter(scope_id, config):
        resources.append(scope_id)
        return echo.State(calls=8)

    async def exit_hook(scope_id, config, state):
        exits.append(scope_id)

    async def mark(repo, *args, **kwargs):
        result = await original(repo, *args, **kwargs)
        repo._db.info["ready_commit"] = True
        return result

    class UnavailableResolution(AsyncSession):
        async def commit(self) -> None:
            nonlocal unavailable
            if self.info.get("ready_commit"):
                unavailable = True
                raise RuntimeError("commit response lost")
            await super().commit()

        async def execute(self, *args, **kwargs):
            if unavailable:
                raise OperationalError("database unavailable", {}, Exception("disconnected"))
            return await super().execute(*args, **kwargs)

    install_layer(monkeypatch, "scoped", layer("scoped", enter_scope=enter, exit_scope=exit_hook))
    monkeypatch.setattr(SessionRepository, "publish_ready", mark)
    service = SessionService(
        async_sessionmaker(database.engine, class_=UnavailableResolution, expire_on_commit=False)
    )
    with pytest.raises(SessionCreationError) as raised:
        await service.create_session(selection(scoped={}))
    session_id = raised.value.session_id
    assert str(raised.value.__cause__) == "commit response lost"
    assert resources == [session_id] and exits == []
    observer = SessionService(database.sessions)
    assert (await observer.get_session(session_id)).status == LifecycleStatus.CREATING
    assert await stored(database, session_id) == {}


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

    install_layer(monkeypatch, "scoped", layer("scoped", exit_scope=exit_hook))
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

    install_layer(monkeypatch, "a", layer("a", exit_scope=exit_a))
    install_layer(monkeypatch, "b", layer("b", exit_scope=exit_b))
    service = SessionService(database.sessions)
    session = await service.create_session(selection(a={}, b={}))
    with pytest.raises(type(error)) as raised:
        await service.close_session(session.id)
    assert raised.value is error
    attempts = 3 if retryable else 1
    assert failures == [session.id] * attempts
    assert await stored(database, session.id) == {"a": {"calls": 1}, "b": {"calls": attempts}}
    assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING
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

    install_layer(monkeypatch, "scoped", layer("scoped", exit_scope=exit_hook))
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

    install_layer(monkeypatch, "scoped", layer("scoped", state=State, exit_scope=exit_hook))
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

    install_layer(monkeypatch, "scoped", layer("scoped", exit_scope=exit_hook))
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

    install_layer(monkeypatch, "scoped", layer("scoped", exit_scope=exit_hook))
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

    install_layer(monkeypatch, "scoped", layer("scoped", exit_scope=exit_hook))
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

    install_layer(monkeypatch, "scoped", layer("scoped", enter_scope=enter))
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


async def test_creating_is_visible_but_not_admitted_and_identity_is_never_replayed(
    database, monkeypatch
):
    entering, finish = asyncio.Event(), asyncio.Event()
    calls = []
    session_id = uuid4()

    async def enter(scope_id, config):
        calls.append(scope_id)
        entering.set()
        await finish.wait()
        return echo.State(calls=9)

    install_layer(monkeypatch, "scoped", layer("scoped", enter_scope=enter))
    service = SessionService(database.sessions)
    data = selection(scoped={"prefix": "fixed"}).model_copy(update={"id": session_id})
    task = asyncio.create_task(service.create_session(data))
    try:
        await asyncio.wait_for(entering.wait(), 5)
        record = await service.get_session(session_id)
        assert record.status == LifecycleStatus.CREATING
        assert record.layers == {"scoped": {"prefix": "fixed", "uppercase": False}}
        assert (await service.list_sessions()).items == [record]
        assert await stored(database, session_id) == {}
        with pytest.raises(SessionBusy):
            await service.close_session(session_id)
        with pytest.raises(SessionAlreadyExists) as busy:
            await service.create_session(data)
        assert busy.value.session_id == session_id
        with pytest.raises(LifecycleError):
            await service.update_session(session_id, UpdateSession(model_settings={}))
        assert (
            await service.update_session(session_id, UpdateSession(title="recoverable"))
        ).title == "recoverable"
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
    assert task.result().status == LifecycleStatus.READY
    assert await stored(database, session_id) == {"scoped": {"calls": 9}}
    with pytest.raises(SessionAlreadyExists):
        await service.create_session(data)
    assert calls == [session_id]


@pytest.mark.parametrize("creation_error", [RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("cleanup_error", [RuntimeError, asyncio.CancelledError])
async def test_failed_creation_cleans_partial_and_uncalled_enters_without_state(
    database, monkeypatch, creation_error, cleanup_error, caplog
):
    class State(BaseModel):
        resource_id: UUID

    resources, exits = set(), []
    failing_exit = True
    error = creation_error("partial enter failed")

    async def enter_a(scope_id, config):
        resources.add((scope_id, "a"))
        return State(resource_id=scope_id)

    async def enter_b(scope_id, config):
        resources.add((scope_id, "b"))
        raise error

    async def enter_c(scope_id, config):
        pytest.fail("Abandoned creation must not enter the remaining layer")

    def exit_for(name):
        async def exit_hook(scope_id, config, state):
            assert state is None
            exits.append((name, scope_id))
            if name == "b" and failing_exit:
                raise cleanup_error("cleanup response lost")
            resources.discard((scope_id, name))

        return exit_hook

    for name, enter in (("a", enter_a), ("b", enter_b), ("c", enter_c)):
        install_layer(
            monkeypatch,
            name,
            layer(name, state=State, enter_scope=enter, exit_scope=exit_for(name)),
        )
    service = SessionService(database.sessions)
    session_id = uuid4()
    expected = SessionCreationError if isinstance(error, Exception) else asyncio.CancelledError
    with pytest.raises(expected) as raised:
        await service.create_session(
            selection(a={}, b={}, c={}).model_copy(update={"id": session_id})
        )
    if isinstance(error, Exception):
        assert isinstance(raised.value, SessionCreationError)
        assert raised.value.session_id == session_id
        assert raised.value.__cause__ is error
    else:
        assert raised.value is error
    assert str(session_id) in caplog.text and "layer b" in caplog.text
    assert exits == [(name, session_id) for name in ("c", "b", "a")]
    assert resources == {(session_id, "b")}
    assert (await service.get_session(session_id)).status == LifecycleStatus.CLOSING
    assert await stored(database, session_id) == {}
    failing_exit = False
    assert (await service.close_session(session_id)).status == LifecycleStatus.CLOSED
    assert exits == [(name, session_id) for name in ("c", "b", "a", "c", "b", "a")]
    assert not resources and await stored(database, session_id) == {}


async def test_lost_creation_owner_cannot_publish_or_compensate_and_new_owner_can_close(
    database, monkeypatch
):
    exits = []

    async def enter(scope_id, config):
        async with database.sessions.begin() as db:
            row = await db.get(SessionLeaseRow, scope_id)
            assert row is not None
            row.lock_token = uuid4()
        return echo.State(calls=7)

    async def exit_hook(scope_id, config, state):
        exits.append((scope_id, state))

    install_layer(monkeypatch, "scoped", layer("scoped", enter_scope=enter, exit_scope=exit_hook))
    service = SessionService(database.sessions)
    with pytest.raises(SessionCreationError) as raised:
        await service.create_session(selection(scoped={}))
    session_id = raised.value.session_id
    assert isinstance(raised.value.__cause__, LeaseLost)
    assert (await service.get_session(session_id)).status == LifecycleStatus.CREATING
    assert await stored(database, session_id) == {} and exits == []
    async with database.sessions.begin() as db:
        row = await db.get(SessionLeaseRow, session_id)
        assert row is not None
        row.lock_token = None
    assert (await service.close_session(session_id)).status == LifecycleStatus.CLOSED
    assert exits == [(session_id, None)]


async def test_http_fixed_identity_conflict_does_not_repeat_enter(database, monkeypatch):
    calls = []

    async def enter(scope_id, config):
        calls.append(scope_id)
        return echo.State()

    install_layer(monkeypatch, "scoped", layer("scoped", enter_scope=enter))
    service = SessionService(database.sessions)
    app = FastAPI()
    app.include_router(create_router(ModelService(database.sessions), service))
    session_id = uuid4()
    body = selection(scoped={}).model_dump(mode="json") | {
        "id": str(session_id),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.post("/api/sessions", json=body)).status_code == 201
        conflict = await client.post("/api/sessions", json=body)
        assert conflict.status_code == 409
        assert conflict.json()["session_id"] == str(session_id)
    assert calls == [session_id]


async def test_ready_corrupt_state_is_rejected_before_any_exit(database, monkeypatch):
    exits = []

    async def exit_hook(scope_id, config, state):
        exits.append(state)

    install_layer(monkeypatch, "scoped", layer("scoped", exit_scope=exit_hook))
    service = SessionService(database.sessions)
    session = await service.create_session(selection(scoped={}))
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session.id)
        assert row is not None
        row.layer_states = {}
    with pytest.raises(ValueError, match="match the configured"):
        await service.close_session(session.id)
    assert exits == []
    assert (await service.get_session(session.id)).status == LifecycleStatus.READY

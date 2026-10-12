"""Real scope Activities keep durable ownership across hooks and compensation."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import BaseModel, model_validator
from pydantic_ai.capabilities import Capability
from temporalio.client import ActivityFailureError

from kapy.control.sessions import (
    CreateSession,
    OperationConflict,
    OperationFailed,
    OperationPhase,
    SessionBusy,
    UpdateSession,
)
from kapy.control.sessions import service as sessions_service
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.operations import SessionOperationRow
from kapy.control.types import utc_now
from kapy.layers.registry import get_layers
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.runner import scopes
from pydantic_ai_layer_protocol import Layer, LayerRegistry, RetryableScopeError
from pydantic_ai_layer_protocol.examples import echo

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def install_layer(
    monkeypatch, layer_id, *, config=echo.Config, state=echo.State, enter=None, exit_hook=None
):
    definitions = dict(sessions_service.layer_registry.definitions)
    definitions[layer_id] = Layer(
        config_type=config,
        state_type=state,
        capability=Capability(id=layer_id),
        enter_scope=enter,
        exit_scope=exit_hook,
    )
    factories = {
        name: (lambda layer_id, access, definition=definition: definition)
        for name, definition in definitions.items()
    }
    registry = LayerRegistry(factories, get_layers=get_layers)
    monkeypatch.setattr(sessions_service, "layer_registry", registry)
    monkeypatch.setattr(scopes, "layer_registry", registry)


def selection(**layers):
    return CreateSession(provider_id=uuid4(), model_name="test", layers=layers)


async def stored(database, session_id):
    async with database.sessions.begin() as db:
        return (await db.get(SessionRow, session_id)).layer_states


async def test_enter_initializes_required_states_and_same_key_returns_original_result(
    database, session_runtime, monkeypatch
):
    class State(BaseModel):
        resource: str

    enters, exits = [], []

    async def enter(session_id, config):
        enters.append((session_id, config.prefix))
        return State(resource=str(session_id))

    async def exit_hook(session_id, config, state):
        exits.append((session_id, state))

    install_layer(monkeypatch, "both", state=State, enter=enter, exit_hook=exit_hook)
    install_layer(monkeypatch, "default")
    service = session_runtime.service(database.sessions)
    key = uuid4()
    data = selection(both={"prefix": "fixed:"}, default={})
    session = await service.create_session(data, activity_id=key)
    assert enters == [(session.id, "fixed:")]
    assert await stored(database, session.id) == {
        "both": {"resource": str(session.id)},
        "default": {"calls": 0},
    }
    assert await service.create_session(data, activity_id=key) == session
    with pytest.raises(OperationConflict):
        await service.create_session(data.model_copy(update={"title": "changed"}), activity_id=key)
    close_key = uuid4()
    closed = await service.close_session(session.id, activity_id=close_key)
    assert closed.status == LifecycleStatus.CLOSED
    assert len(exits) == 1 and exits[0][1].resource == str(session.id)
    assert await service.close_session(session.id, activity_id=close_key) == closed
    assert await service.create_session(data, activity_id=key) == session
    assert len(enters) == len(exits) == 1


@pytest.mark.parametrize("bad", ["unknown", "config", "default_state"])
async def test_validation_precedes_any_enter(database, session_runtime, monkeypatch, bad):
    class RequiredState(BaseModel):
        resource: str

    calls = []

    async def enter(session_id, config):
        calls.append(session_id)
        return echo.State()

    install_layer(monkeypatch, "first", enter=enter)
    install_layer(
        monkeypatch, "second", state=RequiredState if bad == "default_state" else echo.State
    )
    data = selection(
        **{
            "first": {},
            "unknown" if bad == "unknown" else "second": {"unknown": True}
            if bad == "config"
            else {},
        }
    )
    service = session_runtime.service(database.sessions)
    with pytest.raises(ValueError):
        await service.create_session(data, activity_id=uuid4())
    assert not calls and not (await service.list_sessions()).items


@pytest.mark.parametrize("succeeds", [False, True])
async def test_existing_scope_retry_contract_is_bounded_without_activity_retry(
    database, session_runtime, monkeypatch, succeeds
):
    calls = []

    async def enter(session_id, config):
        calls.append((session_id, config.prefix))
        config.prefix = "hook mutation"
        if not succeeds or len(calls) < 3:
            raise RetryableScopeError("allocation failed")
        return echo.State(calls=7)

    install_layer(monkeypatch, "scoped", enter=enter)
    service = session_runtime.service(database.sessions)
    key, session_id = uuid4(), uuid4()
    data = selection(scoped={"prefix": "fixed"}).model_copy(update={"id": session_id})
    if succeeds:
        session = await service.create_session(data, activity_id=key)
        assert session.status == LifecycleStatus.READY
        assert await stored(database, session_id) == {"scoped": {"calls": 7}}
    else:
        with pytest.raises(OperationFailed):
            await service.create_session(data, activity_id=key)
        assert (await service.get_session(session_id)).status == LifecycleStatus.CLOSED
    assert calls == [(session_id, "fixed")] * 3
    assert (await session_runtime.client.get_activity_handle(str(key)).describe()).attempt == 1


async def test_creating_owns_session_and_local_wait_cancellation_does_not_cancel_activity(
    database, session_runtime, monkeypatch
):
    entering, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def enter(session_id, config):
        calls.append(session_id)
        entering.set()
        await release.wait()
        return echo.State(calls=9)

    install_layer(monkeypatch, "scoped", enter=enter)
    service = session_runtime.service(database.sessions)
    key, session_id = uuid4(), uuid4()
    data = selection(scoped={}).model_copy(update={"id": session_id})
    task = asyncio.create_task(service.create_session(data, activity_id=key))
    try:
        await asyncio.wait_for(entering.wait(), 5)
        assert (await service.get_session(session_id)).status == LifecycleStatus.CREATING
        with pytest.raises(SessionBusy):
            await service.close_session(session_id, activity_id=uuid4())
        with pytest.raises(LifecycleError):
            await service.update_session(session_id, UpdateSession(title="busy"))
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        duplicate = asyncio.create_task(service.create_session(data, activity_id=key))
        release.set()
        assert (await duplicate).status == LifecycleStatus.READY
        assert calls == [session_id]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_real_creation_activity_cancellation_compensates_partial_and_uncalled_scopes(
    database, session_runtime, monkeypatch
):
    entering = asyncio.Event()
    exits = []

    async def enter(session_id, config):
        entering.set()
        await asyncio.Future()

    async def exit_hook(session_id, config, state):
        exits.append((config.prefix, state))

    install_layer(monkeypatch, "partial", enter=enter, exit_hook=exit_hook)
    install_layer(monkeypatch, "uncalled", enter=enter, exit_hook=exit_hook)
    service = session_runtime.service(database.sessions)
    key, session_id = uuid4(), uuid4()
    task = asyncio.create_task(
        service.create_session(
            selection(partial={"prefix": "first"}, uncalled={"prefix": "second"}).model_copy(
                update={"id": session_id}
            ),
            activity_id=key,
        )
    )
    await asyncio.wait_for(entering.wait(), 5)
    handle = session_runtime.client.get_activity_handle(str(key))
    await handle.cancel()
    with pytest.raises(ActivityFailureError):
        await handle.result()
    with pytest.raises(OperationFailed):
        await task
    assert exits == [("second", None), ("first", None)]
    assert (await service.get_session(session_id)).status == LifecycleStatus.CLOSED


async def test_creation_compensation_retains_owner_and_failed_close_saves_progress(
    database, session_runtime, monkeypatch
):
    compensating, release = asyncio.Event(), asyncio.Event()
    failing = True

    async def enter(session_id, config):
        raise RuntimeError("partial creation")

    async def exit_hook(session_id, config, state):
        if failing:
            compensating.set()
            await release.wait()
            raise RuntimeError("cleanup failed")

    install_layer(monkeypatch, "scoped", enter=enter, exit_hook=exit_hook)
    service = session_runtime.service(database.sessions)
    key, session_id = uuid4(), uuid4()
    task = asyncio.create_task(
        service.create_session(
            selection(scoped={}).model_copy(update={"id": session_id}), activity_id=key
        )
    )
    try:
        await asyncio.wait_for(compensating.wait(), 5)
        assert (await service.get_session(session_id)).status == LifecycleStatus.CLOSING
        assert (await service.get_operation(session_id, key)).phase == OperationPhase.RUNNING
        with pytest.raises(SessionBusy):
            await service.close_session(session_id, activity_id=uuid4())
    finally:
        release.set()
    with pytest.raises(OperationFailed):
        await task
    assert (await service.get_session(session_id)).status == LifecycleStatus.CLOSING
    failing = False
    assert (
        await service.close_session(session_id, activity_id=uuid4())
    ).status == LifecycleStatus.CLOSED


async def test_deadline_fences_old_creator_before_publish_and_allows_explicit_cleanup(
    database, session_runtime, monkeypatch
):
    entering, release = asyncio.Event(), asyncio.Event()
    exits = []

    async def enter(session_id, config):
        entering.set()
        await release.wait()
        return echo.State(calls=5)

    async def exit_hook(session_id, config, state):
        exits.append(state)

    install_layer(monkeypatch, "scoped", enter=enter, exit_hook=exit_hook)
    service = session_runtime.service(database.sessions)
    key, session_id = uuid4(), uuid4()
    task = asyncio.create_task(
        service.create_session(
            selection(scoped={}).model_copy(update={"id": session_id}), activity_id=key
        )
    )
    try:
        await asyncio.wait_for(entering.wait(), 5)
        async with database.sessions.begin() as db:
            operation = await db.get(SessionOperationRow, key)
            operation.deadline = utc_now() - timedelta(seconds=1)
        assert (await service.get_operation(session_id, key)).phase == OperationPhase.FAILED
        assert (
            await service.close_session(session_id, activity_id=uuid4())
        ).status == LifecycleStatus.CLOSED
        release.set()
        with pytest.raises(OperationFailed):
            await task
        # Wait for the late native result; its stale owner cannot write or compensate.
        with pytest.raises(ActivityFailureError):
            await session_runtime.client.get_activity_handle(str(key)).result()
        assert exits == [None] and await stored(database, session_id) == {}
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


async def test_lost_creation_commit_acknowledgment_keeps_success_without_compensation(
    database, session_runtime, monkeypatch
):
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    from kapy.control.sessions.operations import OperationRepository

    exits = []

    async def enter(session_id, config):
        return echo.State(calls=4)

    async def exit_hook(session_id, config, state):
        exits.append(session_id)

    install_layer(monkeypatch, "scoped", enter=enter, exit_hook=exit_hook)
    original = OperationRepository.complete

    async def mark(repo, *args, **kwargs):
        result = await original(repo, *args, **kwargs)
        repo.db.info["lost_ack"] = True
        return result

    def lost_ack(db):
        if db.info.pop("lost_ack", False):
            raise RuntimeError("commit response lost after durable commit")

    monkeypatch.setattr(OperationRepository, "complete", mark)
    event.listen(Session, "after_commit", lost_ack)
    service = session_runtime.service(database.sessions)
    key = uuid4()
    data = selection(scoped={})
    try:
        session = await service.create_session(data, activity_id=key)
        assert session.status == LifecycleStatus.READY
        assert await stored(database, session.id) == {"scoped": {"calls": 4}}
        assert await service.create_session(data, activity_id=key) == session
        assert not exits
        assert (await service.get_operation(session.id, key)).phase == OperationPhase.SUCCEEDED
    finally:
        event.remove(Session, "after_commit", lost_ack)


async def test_exit_retry_uses_revalidated_saved_state(database, session_runtime, monkeypatch):
    class State(BaseModel):
        count: int = 0

    observed = []

    async def exit_hook(session_id, config, state):
        observed.append((state.count, type(state.count)))
        if len(observed) == 1:
            # Mutation is not auto-validated; persistence normalizes this valid input.
            state.count = "1"
            raise RetryableScopeError("retry safe cleanup")
        assert state.count == 1 and type(state.count) is int
        state.count = 2

    install_layer(monkeypatch, "scoped", state=State, exit_hook=exit_hook)
    service = session_runtime.service(database.sessions)
    session = await service.create_session(selection(scoped={}), activity_id=uuid4())
    with pytest.warns(UserWarning, match="Pydantic serializer warnings"):
        closed = await service.close_session(session.id, activity_id=uuid4())
    assert closed.status == LifecycleStatus.CLOSED
    assert observed == [(0, int), (1, int)]
    assert await stored(database, session.id) == {"scoped": {"count": 2}}


async def test_enter_receives_the_same_validated_config_as_persisted(
    database, session_runtime, monkeypatch
):
    class Config(BaseModel):
        items: list[str]

        @model_validator(mode="before")
        @classmethod
        def normalize(cls, value):
            value["items"].append("validated")
            return value

    received = []

    async def enter(session_id, config):
        received.append(list(config.items))
        config.items.append("hook-local")
        return echo.State()

    install_layer(monkeypatch, "scoped", config=Config, enter=enter)
    service = session_runtime.service(database.sessions)
    data = selection(scoped={"items": ["input"]})
    key = uuid4()
    session = await service.create_session(data, activity_id=key)
    assert received == [["input", "validated"]]
    assert session.layers == {"scoped": {"items": ["input", "validated"]}}
    assert data.layers == {"scoped": {"items": ["input"]}}
    assert await service.create_session(data, activity_id=key) == session
    assert received == [["input", "validated"]]

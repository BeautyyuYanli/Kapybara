"""Public lease contract on real PostgreSQL, without business or runner records."""

import asyncio
from uuid import uuid4

import anyio
import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session
from sqlalchemy.util import await_only

from kapy.agent_runner import RunnerLost
from kapy.session_lease import LeaseLost, SessionBusy, is_session_busy, open_session_lease
from kapy.session_lease import service as lease_service

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def expire(database, session_id):
    async with database.sessions.begin() as db:
        await db.execute(
            text(
                "UPDATE session_leases SET heartbeat_at=clock_timestamp() - interval '1 hour' "
                "WHERE session_id=:id"
            ),
            {"id": session_id},
        )


async def test_independent_scope_release_and_expired_handle(database):
    session_id = uuid4()
    assert RunnerLost is LeaseLost
    async with open_session_lease(session_id, session_factory=database.sessions) as lease:
        lease.check()
        async with database.sessions.begin() as db:
            await lease.lock_owned(db)
            assert await is_session_busy(db, session_id)
            assert (await db.execute(text("SELECT count(*) FROM agent_states"))).scalar_one() == 0
            assert (await db.execute(text("SELECT count(*) FROM sessions"))).scalar_one() == 0
    with pytest.raises(LeaseLost):
        lease.check()
    async with database.sessions.begin() as db:
        assert not await is_session_busy(db, session_id)
        with pytest.raises(LeaseLost):
            await lease.lock_owned(db)


async def test_competing_acquisitions_and_independent_keys(database):
    session_id = uuid4()
    both_attempted = asyncio.Event()
    outcomes = []

    async def claim():
        try:
            async with open_session_lease(session_id, session_factory=database.sessions):
                outcomes.append("acquired")
                await both_attempted.wait()
        except SessionBusy:
            outcomes.append("busy")
            both_attempted.set()

    async with asyncio.timeout(5):
        await asyncio.gather(claim(), claim())
    assert sorted(outcomes) == ["acquired", "busy"]
    async with open_session_lease(session_id, session_factory=database.sessions):
        async with open_session_lease(uuid4(), session_factory=database.sessions):
            pass


async def test_expiration_allows_old_owner_until_takeover_and_notifies_loss(database):
    session_id = uuid4()
    with pytest.raises(LeaseLost):
        async with open_session_lease(session_id, session_factory=database.sessions) as old:
            await expire(database, session_id)
            async with database.sessions.begin() as db:
                assert not await is_session_busy(db, session_id)
                await old.lock_owned(db)
            monitor = asyncio.create_task(old.wait_lost())
            try:
                async with open_session_lease(session_id, session_factory=database.sessions) as new:
                    with pytest.raises(LeaseLost):
                        async with database.sessions.begin() as db:
                            await old.lock_owned(db)
                    with pytest.raises(LeaseLost):
                        await monitor
                    async with database.sessions.begin() as db:
                        await new.lock_owned(db)
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)


async def test_protected_transaction_blocks_takeover_until_commit(database, wait_for_lock):
    session_id = uuid4()
    pid_ready = asyncio.Future()

    async def takeover():
        async with database.engine.connect() as connection:
            pid_ready.set_result(
                (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            )
            await connection.rollback()
            async with open_session_lease(
                session_id, session_factory=async_sessionmaker(connection)
            ) as new:
                new.check()

    with pytest.raises(LeaseLost):
        async with open_session_lease(session_id, session_factory=database.sessions) as old:
            await expire(database, session_id)
            async with database.sessions.begin() as db:
                await old.lock_owned(db)
                task = asyncio.create_task(takeover())
                await wait_for_lock(await pid_ready)
                assert not task.done()
            await asyncio.wait_for(task, 5)


async def test_waiting_ownership_check_rechecks_committed_token(database, wait_for_lock):
    session_id = uuid4()
    pid_ready = asyncio.Future()
    with pytest.raises(LeaseLost):
        async with open_session_lease(session_id, session_factory=database.sessions) as lease:

            async def stale_write():
                async with database.sessions.begin() as db:
                    pid_ready.set_result(
                        (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                    )
                    await lease.lock_owned(db)

            async with database.sessions.begin() as db:
                await db.execute(
                    text("UPDATE session_leases SET lock_token=:token WHERE session_id=:id"),
                    {"id": session_id, "token": uuid4()},
                )
                task = asyncio.create_task(stale_write())
                await wait_for_lock(await pid_ready)
            with pytest.raises(LeaseLost):
                await task


async def test_heartbeat_failure_wakes_monitor_and_survives_context_exit(database, monkeypatch):
    update_owned = lease_service._update_owned

    async def fail_renewal(db, lease, *, release):
        if not release:
            raise OSError("renewal unavailable")
        await update_owned(db, lease, release=True)

    monkeypatch.setattr(lease_service, "_update_owned", fail_renewal)
    session_id = uuid4()
    with pytest.raises(OSError, match="renewal unavailable"):
        async with open_session_lease(
            session_id, session_factory=database.sessions, heartbeat_interval=0.01
        ) as lease:
            with pytest.raises(OSError, match="renewal unavailable"):
                await asyncio.wait_for(lease.wait_lost(), 5)
    async with database.sessions.begin() as db:
        assert not await is_session_busy(db, session_id)


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_cancelled_owner_releases_and_joins_heartbeat(
    database, cancellation, heartbeat_observation
):
    session_id = uuid4()
    heartbeat_tasks, renewed = heartbeat_observation

    async def owner():
        async with open_session_lease(
            session_id, session_factory=database.sessions, heartbeat_interval=0.01
        ):
            await asyncio.Event().wait()

    if cancellation == "asyncio":
        task = asyncio.create_task(owner())
        await asyncio.wait_for(renewed.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with anyio.create_task_group() as group:
            group.start_soon(owner)
            await asyncio.wait_for(renewed.wait(), 5)
            group.cancel_scope.cancel()
    async with database.sessions.begin() as db:
        assert not await is_session_busy(db, session_id)
    assert heartbeat_tasks and all(task.done() for task in heartbeat_tasks)


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
@pytest.mark.parametrize("commit_event", ["before_commit", "after_commit"])
async def test_cancel_during_acquisition_commit_joins_then_releases(
    database, cancellation, commit_event
):
    session_id = uuid4()
    committing, finish = asyncio.Event(), asyncio.Event()
    acquired = False
    cancelled = False

    def pause_first_commit(session):
        if not committing.is_set():
            committing.set()
            await_only(finish.wait())

    async def owner():
        nonlocal acquired, cancelled
        try:
            async with open_session_lease(session_id, session_factory=database.sessions):
                acquired = True
        except asyncio.CancelledError:
            cancelled = True
            raise

    async def wait_at_commit():
        await asyncio.wait_for(committing.wait(), 5)
        if commit_event == "after_commit":
            # Another connection must see the committed token before cancellation;
            # rollback alone cannot clean up this acquisition outcome.
            async with database.sessions.begin() as db:
                assert await is_session_busy(db, session_id)

    event.listen(Session, commit_event, pause_first_commit)
    try:
        if cancellation == "asyncio":
            task = asyncio.create_task(owner())
            try:
                await wait_at_commit()
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            finally:
                finish.set()
                with pytest.raises(asyncio.CancelledError):
                    await task
        else:
            async with anyio.create_task_group() as group:
                group.start_soon(owner)
                await wait_at_commit()
                group.cancel_scope.cancel()
                finish.set()
    finally:
        finish.set()
        event.remove(Session, commit_event, pause_first_commit)
    assert cancelled and not acquired
    async with database.sessions.begin() as db:
        assert not await is_session_busy(db, session_id)
        assert (
            await db.execute(
                text("SELECT lock_token FROM session_leases WHERE session_id=:id"),
                {"id": session_id},
            )
        ).scalar_one_or_none() is None
    async with open_session_lease(session_id, session_factory=database.sessions):
        pass


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_cancel_interrupts_blocked_acquisition_without_unlocking(
    database, cancellation, wait_for_lock
):
    session_id = uuid4()
    async with open_session_lease(session_id, session_factory=database.sessions):
        pass
    pid_ready = asyncio.Future()
    acquired = False
    owner_done = asyncio.Event()
    acquisition_tasks = set()

    def observe_query(conn, cursor, statement, parameters, context, many):
        if statement.startswith("INSERT INTO session_leases"):
            acquisition_tasks.add(asyncio.current_task())
            pid_ready.set_result(conn.connection.driver_connection.info.backend_pid)

    async def owner():
        nonlocal acquired
        try:
            async with open_session_lease(session_id, session_factory=database.sessions):
                acquired = True
        finally:
            owner_done.set()

    event.listen(database.engine.sync_engine, "before_cursor_execute", observe_query)
    try:
        async with database.sessions.begin() as blocker:
            await blocker.execute(
                text("SELECT session_id FROM session_leases WHERE session_id=:id FOR UPDATE"),
                {"id": session_id},
            )
            # Keep the real row lock until the cancelled acquisition has exited.
            if cancellation == "asyncio":
                task = asyncio.create_task(owner())
                try:
                    await wait_for_lock(await asyncio.wait_for(pid_ready, 5))
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 6)
                    assert owner_done.is_set()
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            else:
                async with anyio.create_task_group() as group:
                    group.start_soon(owner)
                    await wait_for_lock(await asyncio.wait_for(pid_ready, 5))
                    group.cancel_scope.cancel()
                    with anyio.CancelScope(shield=True):
                        await asyncio.wait_for(owner_done.wait(), 6)
            assert not acquired
            assert all(task is not None and task.done() for task in acquisition_tasks)
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", observe_query)
    async with database.sessions.begin() as db:
        assert not await is_session_busy(db, session_id)


@pytest.mark.parametrize(
    "interval,lease_timeout", [(0, 60), (1, 1), (2, 1), (float("nan"), 60), (1, float("inf"))]
)
async def test_invalid_policy_rejected_before_acquisition(database, interval, lease_timeout):
    with pytest.raises(ValueError):
        async with open_session_lease(
            uuid4(),
            session_factory=database.sessions,
            heartbeat_interval=interval,
            heartbeat_timeout=lease_timeout,
        ):
            pytest.fail("invalid policy accepted")


async def test_initialization_failure_releases_acquired_lease(database, monkeypatch):
    from pydantic_ai import Agent

    from kapy.agent_runner import open_runner
    from kapy.agent_runner.repository import AgentRepository

    async def fail_resume(self, session_id):
        raise ValueError("bad checkpoint")

    monkeypatch.setattr(AgentRepository, "resume", fail_resume)
    session_id = uuid4()
    with pytest.raises(ValueError, match="bad checkpoint"):
        async with open_runner(session_id, agent=Agent("test"), session_factory=database.sessions):
            pass
    async with database.sessions.begin() as db:
        assert not await is_session_busy(db, session_id)


async def test_cancellation_during_release_waits_for_cleanup_then_propagates(database, monkeypatch):
    session_id = uuid4()
    releasing, finish = asyncio.Event(), asyncio.Event()
    update_owned = lease_service._update_owned

    async def delayed_release(db, lease, *, release):
        if release:
            releasing.set()
            await finish.wait()
        await update_owned(db, lease, release=release)

    monkeypatch.setattr(lease_service, "_update_owned", delayed_release)

    async def owner():
        async with open_session_lease(session_id, session_factory=database.sessions):
            pass

    task = asyncio.create_task(owner())
    await releasing.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with database.sessions.begin() as db:
        assert not await is_session_busy(db, session_id)


async def test_cleanup_error_does_not_hide_foreground_failure(database, monkeypatch):
    update_owned = lease_service._update_owned

    async def fail_release(db, lease, *, release):
        if release:
            raise OSError("release unavailable")
        await update_owned(db, lease, release=release)

    monkeypatch.setattr(lease_service, "_update_owned", fail_release)
    with pytest.raises(ValueError, match="foreground") as caught:
        async with open_session_lease(uuid4(), session_factory=database.sessions):
            raise ValueError("foreground")
    assert any("release unavailable" in note for note in caught.value.__notes__)


async def test_heartbeat_covers_runner_native_graph_cleanup(database, heartbeat_observation):
    from pydantic_ai import Agent, RunContext
    from pydantic_ai.toolsets import FunctionToolset

    from kapy.agent_runner import open_runner

    session_id = uuid4()
    closing, finish = asyncio.Event(), asyncio.Event()
    _, renewed = heartbeat_observation

    class SlowClose(FunctionToolset):
        async def __aexit__(self, *args):
            closing.set()
            await finish.wait()
            return await super().__aexit__(*args)

    toolset = SlowClose()

    @toolset.tool
    def work(ctx: RunContext) -> str:
        return "done"

    async def owner():
        async with open_runner(
            session_id,
            agent=Agent("test", toolsets=[toolset]),
            session_factory=database.sessions,
            heartbeat_interval=0.01,
        ) as runner:
            await runner.rebuild_context()
            assert not (await runner.turn(steer=["go"])).finished

    task = asyncio.create_task(owner())
    try:
        await asyncio.wait_for(closing.wait(), 5)
        renewed.clear()
        await asyncio.wait_for(renewed.wait(), 5)
        with pytest.raises(SessionBusy):
            async with open_session_lease(session_id, session_factory=database.sessions):
                pytest.fail("graph cleanup lost exclusion")
    finally:
        finish.set()
        await task

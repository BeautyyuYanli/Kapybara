"""Business snapshots and full native writer identity commit atomically."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.control.sessions.types import OwnershipLost, SessionBusy
from kapy.lifecycle import LifecycleStatus

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def running(database, session_id):
    key, run_id = uuid4(), str(uuid4())
    async with database.sessions.begin() as db:
        await SessionRepository(db).claim_run(session_id, key, run_id)
    return key, run_id


async def finish(repo, session_id, key, run_id, *, success):
    return await repo.complete(
        session_id,
        key,
        run_id,
        (LifecycleStatus.READY,),
        LifecycleStatus.READY if success else LifecycleStatus.CLOSING,
        runner_state="opaque 摘要" if success else None,
        layer_states={} if success else None,
    )


async def test_snapshot_and_states_commit_with_ownership_release(database, seed_session):
    session_id = uuid4()
    await seed_session(session_id)
    key, run_id = await running(database, session_id)
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.sessions.begin() as db:
            await finish(SessionRepository(db), session_id, key, run_id, success=True)
            raise RuntimeError("rollback")
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        assert row.runner_state is None
        assert (row.current_activity_id, row.current_activity_run_id) == (key, run_id)
        result = await finish(SessionRepository(db), session_id, key, run_id, success=True)
        assert result.current_activity_id is result.current_activity_run_id is None
    with pytest.raises(OwnershipLost):
        async with database.sessions.begin() as db:
            await finish(SessionRepository(db), session_id, key, run_id, success=False)
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        assert row.status == LifecycleStatus.READY and row.runner_state == "opaque 摘要"
        assert (
            "runner_state" not in (await SessionRepository(db).get_session(session_id)).model_dump()
        )


@pytest.mark.parametrize("winner", [True, False], ids=["success", "failure"])
async def test_success_and_failure_race_commits_one_business_outcome(
    database,
    seed_session,
    wait_for_lock,
    winner,
):
    session_id = uuid4()
    await seed_session(session_id)
    key, run_id = await running(database, session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    contender_pid = asyncio.get_running_loop().create_future()

    async def terminal(success, hold=False):
        async with database.sessions.begin() as db:
            if not hold:
                contender_pid.set_result(
                    (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
                )
            await finish(SessionRepository(db), session_id, key, run_id, success=success)
            if hold:
                entered.set()
                await release.wait()

    first = asyncio.create_task(terminal(winner, True))
    await entered.wait()
    second = asyncio.create_task(terminal(not winner))
    try:
        await wait_for_lock(await contender_pid)
        release.set()
        await first
        with pytest.raises(OwnershipLost):
            await second
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        assert row.status == (LifecycleStatus.READY if winner else LifecycleStatus.CLOSING)
        assert row.runner_state == ("opaque 摘要" if winner else None)
        assert row.current_activity_id is row.current_activity_run_id is None


async def test_same_uuid_new_native_run_is_a_different_writer(database, seed_session):
    session_id = uuid4()
    await seed_session(session_id)
    key, old_run = await running(database, session_id)
    new_run = str(uuid4())
    async with database.sessions.begin() as db:
        row = await SessionRepository(db).claim_close_or_run(
            session_id,
            key,
            new_run,
            close=True,
            previous=(key, old_run, LifecycleStatus.READY),
        )
        assert row is not None and row.current_activity_run_id == new_run
    with pytest.raises(OwnershipLost):
        async with database.sessions.begin() as db:
            await SessionRepository(db).guard(session_id, key, old_run, (LifecycleStatus.CLOSING,))
    async with database.sessions.begin() as db:
        assert (
            await SessionRepository(db).claim_close_or_run(
                session_id,
                uuid4(),
                str(uuid4()),
                close=True,
                previous=(key, old_run, LifecycleStatus.READY),
            )
            is None
        )
        with pytest.raises(SessionBusy):
            await SessionRepository(db).update_session(session_id, {"title": "late edit"})


@pytest.mark.parametrize(
    "field,value", [("current_activity_id", uuid4()), ("current_activity_run_id", "run")]
)
async def test_identity_fields_must_be_present_together(database, seed_session, field, value):
    session_id = uuid4()
    await seed_session(session_id)
    with pytest.raises(IntegrityError):
        async with database.sessions.begin() as db:
            row = await db.get(SessionRow, session_id)
            setattr(row, field, value)

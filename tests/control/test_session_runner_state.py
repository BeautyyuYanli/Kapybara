"""Opaque snapshots and ownership outcomes commit atomically in short transactions."""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from kapy.control.sessions import OperationKind, OperationPhase
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.operations import OperationRepository, SessionOperationRow
from kapy.control.sessions.repository import SessionRepository
from kapy.control.sessions.types import OwnershipLost
from kapy.control.types import utc_now
from kapy.lifecycle import LifecycleStatus

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def running(database, session_id):
    key = uuid4()
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        row.status, row.current_activity_id = LifecycleStatus.RUNNING, key
        db.add(
            SessionOperationRow(
                activity_id=key,
                session_id=session_id,
                kind=OperationKind.RUN,
                phase=OperationPhase.RUNNING,
                request={},
                deadline=utc_now() + timedelta(hours=1),
            )
        )
    return key


async def test_final_snapshot_and_result_roll_back_together_and_success_is_immutable(
    database, seed_session
):
    session_id = uuid4()
    await seed_session(session_id)
    key = await running(database, session_id)
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.sessions.begin() as db:
            await OperationRepository(db).complete(
                key, LifecycleStatus.READY, "output", runner_state="opaque 摘要", layer_states={}
            )
            raise RuntimeError("rollback")
    async with database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(session_id) is None
        assert (await OperationRepository(db).get(key)).phase == OperationPhase.RUNNING
        await OperationRepository(db).complete(
            key, LifecycleStatus.READY, "output", runner_state="opaque 摘要", layer_states={}
        )
    with pytest.raises(OwnershipLost):
        async with database.sessions.begin() as db:
            await OperationRepository(db).fail(key, "late failure", expired=True)
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        assert row.status == LifecycleStatus.READY and row.runner_state == "opaque 摘要"
        operation = await OperationRepository(db).get(key)
        assert operation.phase == OperationPhase.SUCCEEDED and operation.result == "output"
        assert (
            "runner_state" not in (await SessionRepository(db).get_session(session_id)).model_dump()
        )


@pytest.mark.parametrize("winner", ["complete", "fail"])
async def test_racing_success_and_failure_publish_one_consistent_terminal_state(
    database, seed_session, winner
):
    session_id = uuid4()
    await seed_session(session_id)
    key = await running(database, session_id)
    entered, release = asyncio.Event(), asyncio.Event()
    winner_pid = asyncio.get_running_loop().create_future()
    contender_pid = asyncio.get_running_loop().create_future()

    async def finish(kind, hold=False):
        async with database.sessions.begin() as db:
            pid = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            (winner_pid if hold else contender_pid).set_result(pid)
            repo = OperationRepository(db)
            if kind == "complete":
                await repo.complete(key, LifecycleStatus.READY, "output", runner_state="messages")
            else:
                await repo.fail(key, "failed", expired=True)
            if hold:
                entered.set()
                await release.wait()

    first = asyncio.create_task(finish(winner, True))
    await entered.wait()
    second = asyncio.create_task(finish("fail" if winner == "complete" else "complete"))
    try:
        owner, contender = await winner_pid, await contender_pid
        async with asyncio.timeout(5):
            while True:
                async with database.sessions.begin() as db:
                    blocked = (
                        await db.execute(
                            text("SELECT :owner = ANY(pg_blocking_pids(:contender))"),
                            {"owner": owner, "contender": contender},
                        )
                    ).scalar_one()
                if blocked:
                    break
                await asyncio.sleep(0.01)
        # The contender's UPDATE began while the first terminal result was still
        # uncommitted. Failure-first makes its allowed closing row pass EPQ while
        # the operation subquery still sees the old running snapshot.
        assert not second.done()
        release.set()
        await first
        with pytest.raises(OwnershipLost):
            await second
    finally:
        release.set()
        await asyncio.gather(first, second, return_exceptions=True)
    async with database.sessions.begin() as db:
        row, operation = (
            await db.get(SessionRow, session_id),
            await OperationRepository(db).get(key),
        )
        if winner == "complete":
            assert row.status == LifecycleStatus.READY and row.runner_state == "messages"
            assert operation.phase == OperationPhase.SUCCEEDED and operation.result == "output"
        else:
            assert row.status == LifecycleStatus.CLOSING and row.runner_state is None
            assert operation.phase == OperationPhase.FAILED and operation.result is None

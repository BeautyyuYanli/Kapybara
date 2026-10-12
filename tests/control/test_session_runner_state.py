"""Opaque snapshots use real row locks, borrowed transactions and exact retry identity."""

import asyncio
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from kapy.control.sessions import CreateSession, SessionService
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.control.sessions.types import UpdateSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_state_defaults_retries_and_ordinary_session_operations(database):
    service = SessionService(database.sessions)
    record = await service.create_session(CreateSession(provider_id=uuid4(), model_name="test"))
    session_id = record.id
    async with database.sessions.begin() as db:
        repo = SessionRepository(db)
        assert await repo.read_runner_state(session_id) == (None, 0)
        await repo.save_runner_state(session_id, expected_version=0, runner_state="opaque 摘要")
        saved_at = (await repo.get_session(session_id)).updated_at
    async with database.sessions.begin() as db:
        repo = SessionRepository(db)
        await repo.save_runner_state(session_id, expected_version=0, runner_state="opaque 摘要")
        assert (await repo.get_session(session_id)).updated_at == saved_at
        assert await repo.read_runner_state(session_id) == ("opaque 摘要", 1)
        await repo.save_runner_state(session_id, expected_version=1, runner_state="")
    await service.update_session(session_id, UpdateSession(title="renamed"))
    await service.close_session(session_id)
    async with database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(session_id) == ("", 2)
    result = (await service.get_session(session_id)).model_dump()
    assert "runner_state" not in result and "runner_state_version" not in result
    for dto in (CreateSession, UpdateSession):
        with pytest.raises(ValidationError):
            dto.model_validate({"provider_id": uuid4(), "model_name": "test", "runner_state": "x"})


async def test_state_conflicts_missing_rows_and_rollback(database, seed_session):
    session_id = uuid4()
    await seed_session(session_id)
    async with database.sessions.begin() as db:
        await SessionRepository(db).save_runner_state(
            session_id, expected_version=0, runner_state='{"value": 1}'
        )
    for version, state in ((0, '{"value":1}'), (2, "future"), (-1, "invalid")):
        with pytest.raises(ValueError):
            async with database.sessions.begin() as db:
                await SessionRepository(db).save_runner_state(
                    session_id, expected_version=version, runner_state=state
                )
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.sessions.begin() as db:
            await SessionRepository(db).save_runner_state(
                session_id, expected_version=1, runner_state="rolled back"
            )
            raise RuntimeError("rollback")
    async with database.sessions.begin() as db:
        repo = SessionRepository(db)
        assert await repo.read_runner_state(session_id) == ('{"value": 1}', 1)
        await repo.save_runner_state(session_id, expected_version=1, runner_state="new")
    with pytest.raises(ValueError):
        async with database.sessions.begin() as db:
            await SessionRepository(db).save_runner_state(
                session_id, expected_version=0, runner_state='{"value": 1}'
            )
    missing = uuid4()
    async with database.sessions.begin() as db:
        repo = SessionRepository(db)
        with pytest.raises(LookupError):
            await repo.read_runner_state(missing)
        with pytest.raises(LookupError):
            await repo.save_runner_state(missing, expected_version=0, runner_state="missing")
        assert await db.get(SessionRow, missing) is None


@pytest.mark.parametrize("same_state", [False, True])
async def test_concurrent_submissions_share_one_version(
    database, seed_session, wait_for_lock, same_state
):
    session_id = uuid4()
    await seed_session(session_id)
    pid_ready = asyncio.Future()

    async def contender():
        async with database.sessions.begin() as db:
            # Retaining this pre-lock ORM row reproduces stale identity-map reads.
            row = await db.get(SessionRow, session_id)
            assert row is not None and row.runner_state_version == 0
            pid_ready.set_result((await db.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            await SessionRepository(db).save_runner_state(
                session_id, expected_version=0, runner_state="winner" if same_state else "loser"
            )

    async with database.sessions.begin() as db:
        await SessionRepository(db).save_runner_state(
            session_id, expected_version=0, runner_state="winner"
        )
        task = asyncio.create_task(contender())
        await wait_for_lock(await pid_ready)
    if same_state:
        await asyncio.wait_for(task, 5)
    else:
        with pytest.raises(ValueError):
            await asyncio.wait_for(task, 5)
    async with database.sessions.begin() as db:
        assert await SessionRepository(db).read_runner_state(session_id) == ("winner", 1)

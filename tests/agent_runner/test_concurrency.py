"""Lease claims use independent PostgreSQL connections, including separate processes."""

import asyncio
import sys
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import text

from kapy.agent_runner import InputBatch, RunnerExecution, RunnerLost, SessionBusy, open_runner
from kapy.agent_runner.repository import AgentRepository
from kapy.control.sessions import SessionService
from kapy.session_lease import open_session_lease
from kapy.session_lease import service as lease_service

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def expire(database, session_id):
    async with database.sessions.begin() as db:
        await db.execute(
            text(
                "UPDATE session_leases SET heartbeat_at = clock_timestamp() - interval '1 hour' "
                "WHERE session_id=:id"
            ),
            {"id": session_id},
        )


async def test_heartbeat_while_model_waits_uses_independent_short_transactions(database):
    entered, finish = asyncio.Event(), asyncio.Event()
    session_id = uuid4()

    async def model(messages, info):
        entered.set()
        await finish.wait()

        return ModelResponse(parts=[TextPart("ok")])

    async def execute():
        async with open_runner(
            session_id,
            agent=Agent(FunctionModel(model)),
            session_factory=database.sessions,
            heartbeat_interval=0.01,
            heartbeat_timeout=0.2,
        ) as runner:
            await runner.rebuild_context()
            return await runner.turn(steer=["go"])

    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        # Expiring the timestamp is a deterministic way to observe the next
        # heartbeat; no model-side transaction holds the execution state row.
        await expire(database, session_id)
        async with asyncio.timeout(5):
            while True:
                async with database.sessions.begin() as db:
                    renewed = (
                        await db.execute(
                            text(
                                "SELECT heartbeat_at > clock_timestamp() - interval '1 second' "
                                "FROM session_leases WHERE session_id=:id"
                            ),
                            {"id": session_id},
                        )
                    ).scalar_one()
                if renewed:
                    break
                await asyncio.sleep(0.005)
        with pytest.raises(SessionBusy):
            async with open_runner(
                session_id,
                agent=Agent(TestModel()),
                session_factory=database.sessions,
                heartbeat_interval=0.01,
                heartbeat_timeout=0.2,
            ):
                pytest.fail("live runner was taken over")
    finally:
        finish.set()
        assert (await asyncio.wait_for(task, 5)).finished


async def test_lost_runner_cancels_external_wait_and_cannot_write_or_release_new_token(
    database, toolset_lifecycle, heartbeat_observation
):
    entered, finish = asyncio.Event(), asyncio.Event()
    session_id, new = uuid4(), uuid4()
    heartbeat_tasks, heartbeat_called = heartbeat_observation

    async def model(messages, info):
        entered.set()
        await finish.wait()

        return ModelResponse(parts=[TextPart("stale")])

    async def execute():
        async with open_runner(
            session_id,
            agent=Agent(FunctionModel(model), toolsets=[toolset_lifecycle]),
            session_factory=database.sessions,
            heartbeat_interval=0.01,
            heartbeat_timeout=60,
        ) as runner:
            await runner.rebuild_context()
            await runner.turn(steer=["go"])

    task = asyncio.create_task(execute())
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(heartbeat_called.wait(), 5)
    assert toolset_lifecycle.events == ["enter"]
    async with database.sessions.begin() as db:
        await db.execute(
            text(
                "UPDATE session_leases SET heartbeat_at=clock_timestamp() - interval '1 hour' "
                "WHERE session_id=:id"
            ),
            {"id": session_id},
        )
        await db.execute(
            text("UPDATE session_leases SET lock_token=:token WHERE session_id=:id"),
            {"id": session_id, "token": new},
        )
    with pytest.raises(RunnerLost):
        await asyncio.wait_for(task, 5)
    assert toolset_lifecycle.events == ["enter", "exit"]
    assert heartbeat_tasks and all(task.done() for task in heartbeat_tasks)
    async with database.sessions.begin() as db:
        repo = AgentRepository(db)
        assert (
            await db.execute(
                text("SELECT lock_token FROM session_leases WHERE session_id=:id"),
                {"id": session_id},
            )
        ).scalar_one() == new
        assert (
            len(
                [
                    message
                    for _, message in await repo.read_history(
                        session_id, start_seq=0, through_seq=2**31 - 1
                    )
                ]
            )
            == 1
        )


async def test_process_exit_leaves_lease_then_allows_takeover(database):
    session_id = uuid4()
    code = """
import asyncio, sys
from uuid import UUID, uuid4
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from kapy.session_lease import open_session_lease
async def main():
    engine=create_async_engine(sys.argv[1].replace("postgresql://", "postgresql+psycopg://", 1),
        connect_args={"options": "-csearch_path="+sys.argv[2]+",pg_catalog"})
    async with open_session_lease(UUID(sys.argv[3]), session_factory=async_sessionmaker(engine)):
        print("acquired", flush=True)
        await asyncio.Event().wait()
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        code,
        database.url,
        database.schema,
        str(session_id),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        assert await asyncio.wait_for(child.stdout.readline(), 10) == b"acquired\n"
        with pytest.raises(SessionBusy):
            async with open_session_lease(session_id, session_factory=database.sessions):
                pytest.fail("live lease was acquired")
        child.kill()
        await child.wait()
        with pytest.raises(SessionBusy):
            async with open_session_lease(session_id, session_factory=database.sessions):
                pytest.fail("live lease was acquired")
        await expire(database, session_id)
        async with open_session_lease(
            session_id, session_factory=database.sessions, takeover_grace_period=0.01
        ):
            pass
    finally:
        if child.returncode is None:
            child.kill()
        await child.communicate()


async def test_caller_cancel_during_context_exit_propagates_and_releases(database):
    session_id = uuid4()
    agent = Agent(TestModel())

    @agent.tool_plain
    def work() -> str:
        return "ok"

    async def execute():
        async with open_runner(
            session_id, agent=agent, session_factory=database.sessions
        ) as runner:
            # Keep a native run open at the next request checkpoint during close.
            await runner.rebuild_context()
            assert not (await runner.turn(steer=["go"])).finished
            owner = asyncio.current_task()
            assert owner is not None
            asyncio.get_running_loop().call_soon(owner.cancel)

    task = asyncio.create_task(execute())
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    async with database.sessions.begin() as db:
        token = (
            await db.execute(
                text("SELECT lock_token FROM session_leases WHERE session_id=:id"),
                {"id": session_id},
            )
        ).scalar_one()
        assert token is None
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert (await runner.turn()).finished


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_heartbeat_cancels_model_and_groups_factory_cleanup_failure(
    database, monkeypatch, cleanup_fails
):
    session_id = uuid4()
    model_entered = asyncio.Event()
    update_owned = lease_service._update_owned

    async def failing_heartbeat(db, lease, *, release):
        if release:
            return await update_owned(db, lease, release=True)
        await model_entered.wait()
        raise OSError("heartbeat connection failed")

    monkeypatch.setattr(lease_service, "_update_owned", failing_heartbeat)

    async def model(messages, info):
        model_entered.set()
        await asyncio.Event().wait()
        return ModelResponse(parts=[TextPart("unreachable")])

    @asynccontextmanager
    async def factory(lease):
        try:
            yield RunnerExecution(Agent(FunctionModel(model)))
        finally:
            if cleanup_fails:
                raise ValueError("foreground cleanup failed")

    error_type = ExceptionGroup if cleanup_fails else OSError
    with pytest.raises(error_type) as caught:
        async with open_runner(
            session_id,
            execution_factory=factory,
            session_factory=database.sessions,
            heartbeat_interval=0.01,
        ) as runner:
            await runner.rebuild_context()
            await runner.turn(steer=["go"])
    if cleanup_fails:

        def leaves(error):
            if isinstance(error, BaseExceptionGroup):
                return [leaf for child in error.exceptions for leaf in leaves(child)]
            return [error]

        assert {(type(e), str(e)) for e in leaves(caught.value)} == {
            (OSError, "heartbeat connection failed"),
            (ValueError, "foreground cleanup failed"),
        }
    else:
        assert str(caught.value) == "heartbeat connection failed"
    async with database.sessions.begin() as db:
        messages = [
            message
            for _, message in await AgentRepository(db).read_history(
                session_id, start_seq=0, through_seq=2**31 - 1
            )
        ]
        assert len(messages) == 1 and isinstance(messages[0], ModelRequest)
        assert (
            await db.execute(
                text("SELECT lock_token FROM session_leases WHERE session_id=:id"),
                {"id": session_id},
            )
        ).scalar_one() is None


@pytest.mark.parametrize("takeover_at", ["input_preparation", "cancel_boundary"])
async def test_runner_rejects_consumption_after_takeover(database, takeover_at, seed_session):
    session_id, new_token = uuid4(), uuid4()
    await seed_session(session_id)
    sessions = SessionService(database.sessions)
    pending = await sessions.enqueue_input(session_id, "steer", "keep pending")
    agent = Agent(TestModel())
    cancel_consumptions = []
    input_consumptions = []

    @agent.tool_plain
    def work() -> str:
        return "ok"

    async def take_over():
        await sessions.request_cancel(session_id)
        async with database.sessions.begin() as db:
            await db.execute(
                text(
                    "UPDATE session_leases SET heartbeat_at=clock_timestamp() - interval '1 hour' "
                    "WHERE session_id=:id"
                ),
                {"id": session_id},
            )
            await db.execute(
                text("UPDATE session_leases SET lock_token=:token WHERE session_id=:id"),
                {"id": session_id, "token": new_token},
            )

    @agent.system_prompt(dynamic=True)
    async def dynamic() -> str:
        if takeover_at == "input_preparation":
            await take_over()
        return "system"

    async def read_steer():
        rows = await sessions.read_inputs(session_id, "steer")
        if not rows:
            return None

        async def consume(db):
            input_consumptions.append(True)
            accepted = await sessions.consume_inputs(
                session_id, "steer", db=db, ids=[row.id for row in rows]
            )
            return tuple(row.content for row in accepted)

        return InputBatch(tuple(row.content for row in rows), consume)

    async def consume_cancel(db):
        result = await sessions.consume_cancel(session_id, db=db)
        cancel_consumptions.append(result)
        return result

    with pytest.raises(RunnerLost):
        async with open_runner(
            session_id, agent=agent, session_factory=database.sessions
        ) as runner:
            await runner.rebuild_context()
            if takeover_at == "cancel_boundary":
                assert not (await runner.turn(steer=["already accepted"])).finished
                await take_over()
            await runner.run(read_steer=read_steer, consume_cancel=consume_cancel)

    assert input_consumptions == []
    assert cancel_consumptions == ([False] if takeover_at == "input_preparation" else [])
    assert [row.id for row in await sessions.read_inputs(session_id, "steer")] == [pending.id]
    assert await sessions.read_cancel(session_id)
    async with database.sessions.begin() as db:
        repo = AgentRepository(db)
        assert (
            await db.execute(
                text("SELECT lock_token FROM session_leases WHERE session_id=:id"),
                {"id": session_id},
            )
        ).scalar_one() == new_token
        messages = [
            message
            for _, message in await repo.read_history(
                session_id, start_seq=0, through_seq=2**31 - 1
            )
        ]
    assert len(messages) == (0 if takeover_at == "input_preparation" else 3)

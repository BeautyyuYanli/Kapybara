"""Lease claims use independent PostgreSQL connections, including separate processes."""

import asyncio
import sys
from uuid import uuid4

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import text

from kapy.tmpv2.agent_runner import InputBatch, RunnerLost, SessionBusy, open_runner
from kapy.tmpv2.agent_runner.repository import AgentRepository
from kapy.tmpv2.control.sessions import SessionService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def expire(database, session_id):
    async with database.sessions.begin() as db:
        await db.execute(
            text(
                "UPDATE agent_states SET heartbeat_at = clock_timestamp() - interval '1 hour' "
                "WHERE session_id=:id"
            ),
            {"id": session_id},
        )


async def acquire(database, session_id, token):
    async with database.sessions.begin() as db:
        return await AgentRepository(db).acquire(session_id, token, heartbeat_timeout=60)


async def wait_for_lock(database, pid):
    async with asyncio.timeout(5):
        while True:
            async with database.sessions.begin() as db:
                waiting = (
                    await db.execute(
                        text(
                            "SELECT wait_event_type = 'Lock' FROM pg_stat_activity WHERE pid=:pid"
                        ),
                        {"pid": pid},
                    )
                ).scalar_one()
            if waiting:
                return
            await asyncio.sleep(0.01)


async def test_concurrent_claim_only_one_winner_and_other_session_independent(database):
    session_id = uuid4()
    results = await asyncio.gather(
        acquire(database, session_id, uuid4()),
        acquire(database, session_id, uuid4()),
        return_exceptions=True,
    )
    assert sum(isinstance(result, SessionBusy) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    assert (await acquire(database, uuid4(), uuid4())).next_step == "done"


async def test_expiration_does_not_revoke_and_takeover_rejects_old_token(database):
    session_id, old, new = uuid4(), uuid4(), uuid4()
    await acquire(database, session_id, old)
    await expire(database, session_id)
    async with database.sessions.begin() as db:
        repo = AgentRepository(db)
        await repo.lock_owned(session_id, old)
        await repo.save_checkpoint(session_id, next_step="done", start_seq=0)
        await repo.heartbeat(session_id, old)
    with pytest.raises(SessionBusy):
        await acquire(database, session_id, new)
    await expire(database, session_id)
    await acquire(database, session_id, new)
    with pytest.raises(RunnerLost):
        async with database.sessions.begin() as db:
            await AgentRepository(db).heartbeat(session_id, old)
    with pytest.raises(RunnerLost):
        async with database.sessions.begin() as db:
            await AgentRepository(db).lock_owned(session_id, old)
    async with database.sessions.begin() as db:
        repo = AgentRepository(db)
        await repo.release(session_id, old)
        await repo.lock_owned(session_id, new)


async def test_lock_owned_protects_transaction_until_commit(database):
    session_id, old, new = uuid4(), uuid4(), uuid4()
    await acquire(database, session_id, old)
    await expire(database, session_id)
    pid_ready = asyncio.Future()

    async def take_over():
        async with database.sessions.begin() as db:
            pid_ready.set_result((await db.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            return await AgentRepository(db).acquire(session_id, new, heartbeat_timeout=60)

    async with database.sessions.begin() as db:
        repo = AgentRepository(db)
        await repo.lock_owned(session_id, old)
        task = asyncio.create_task(take_over())
        pid = await pid_ready
        await wait_for_lock(database, pid)
        assert not task.done()
        await repo.save_checkpoint(
            session_id,
            next_step="model_request",
            start_seq=0,
            messages=[ModelRequest(parts=[UserPromptPart("protected")])],
        )
    state = await asyncio.wait_for(task, 5)
    assert state.next_step == "model_request"
    assert state.next_seq == 1
    async with database.sessions.begin() as db:
        rows = await AgentRepository(db).read_history(session_id, start_seq=0, through_seq=0)
    part = rows[0][1].parts[0]
    assert isinstance(part, UserPromptPart)
    assert part.content == "protected"


async def test_waiting_lock_owned_rechecks_replaced_token(database):
    session_id, old, new = uuid4(), uuid4(), uuid4()
    await acquire(database, session_id, old)
    await expire(database, session_id)
    pid_ready = asyncio.Future()

    async def stale_write():
        async with database.sessions.begin() as db:
            pid_ready.set_result((await db.execute(text("SELECT pg_backend_pid()"))).scalar_one())
            await AgentRepository(db).lock_owned(session_id, old)

    async with database.sessions.begin() as db:
        await AgentRepository(db).acquire(session_id, new, heartbeat_timeout=60)
        task = asyncio.create_task(stale_write())
        await wait_for_lock(database, await pid_ready)
    with pytest.raises(RunnerLost):
        await task


async def test_heartbeat_while_model_waits_uses_independent_short_transactions(database):
    entered, finish = asyncio.Event(), asyncio.Event()
    session_id = uuid4()

    async def model(messages, info):
        entered.set()
        await finish.wait()
        from pydantic_ai.messages import ModelResponse, TextPart

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
                                "FROM agent_states WHERE session_id=:id"
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


async def test_lost_runner_finishes_external_wait_but_cannot_write_or_release_new_token(
    database, toolset_lifecycle, heartbeat_observation
):
    entered, finish = asyncio.Event(), asyncio.Event()
    session_id, new = uuid4(), uuid4()
    heartbeat_tasks, heartbeat_called = heartbeat_observation

    async def model(messages, info):
        entered.set()
        await finish.wait()
        from pydantic_ai.messages import ModelResponse, TextPart

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
                "UPDATE agent_states SET heartbeat_at=clock_timestamp() - interval '1 hour' "
                "WHERE session_id=:id"
            ),
            {"id": session_id},
        )
        await AgentRepository(db).acquire(session_id, new, heartbeat_timeout=60)
    finish.set()
    with pytest.raises(RunnerLost):
        await asyncio.wait_for(task, 5)
    assert toolset_lifecycle.events == ["enter", "exit"]
    assert heartbeat_tasks and all(task.done() for task in heartbeat_tasks)
    async with database.sessions.begin() as db:
        repo = AgentRepository(db)
        await repo.lock_owned(session_id, new)
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
    session_id, new = uuid4(), uuid4()
    code = """
import asyncio, sys
from uuid import UUID, uuid4
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from kapy.tmpv2.agent_runner.repository import AgentRepository
async def main():
    engine=create_async_engine(sys.argv[1].replace("postgresql://", "postgresql+psycopg://", 1),
        connect_args={"options": "-csearch_path="+sys.argv[2]+",pg_catalog"})
    async with async_sessionmaker(engine).begin() as db:
        await AgentRepository(db).acquire(UUID(sys.argv[3]), uuid4(), heartbeat_timeout=60)
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
            await acquire(database, session_id, new)
        child.kill()
        await child.wait()
        with pytest.raises(SessionBusy):
            await acquire(database, session_id, new)
        await expire(database, session_id)
        assert (await acquire(database, session_id, new)).next_step == "done"
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
                text("SELECT lock_token FROM agent_states WHERE session_id=:id"),
                {"id": session_id},
            )
        ).scalar_one()
        assert token is None
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert (await runner.turn()).finished


@pytest.mark.parametrize("failure_source", ["model", "context", "heartbeat"])
async def test_heartbeat_error_remains_visible_when_foreground_also_fails(
    database, monkeypatch, failure_source
):
    session_id = uuid4()
    heartbeat_failed = asyncio.Event()
    model_entered = asyncio.Event()

    async def failing_heartbeat(self, session_id, token):
        await model_entered.wait()
        # Signal completion only after the runner has recorded the background
        # error and the heartbeat's transaction has finished rolling back.
        task = asyncio.current_task()
        assert task is not None
        task.add_done_callback(lambda _: heartbeat_failed.set())
        raise OSError("heartbeat connection failed")

    monkeypatch.setattr(AgentRepository, "heartbeat", failing_heartbeat)

    async def model(messages, info):
        model_entered.set()
        await heartbeat_failed.wait()
        if failure_source == "heartbeat":
            return ModelResponse(parts=[TextPart("must not be committed")])
        raise ValueError("foreground failed")

    error_type = OSError if failure_source == "heartbeat" else ValueError
    error_text = (
        "heartbeat connection failed" if failure_source == "heartbeat" else "foreground failed"
    )
    with pytest.raises(error_type, match=error_text) as caught:
        async with open_runner(
            session_id,
            agent=Agent(FunctionModel(model)),
            session_factory=database.sessions,
            heartbeat_interval=0.01,
            heartbeat_timeout=60,
        ) as runner:
            await runner.rebuild_context()
            if failure_source != "context":
                await runner.turn(steer=["go"])
            else:
                model_entered.set()
                await heartbeat_failed.wait()
                raise ValueError("foreground failed")
    if failure_source != "heartbeat":
        assert any(
            "heartbeat connection failed" in note for note in getattr(caught.value, "__notes__", ())
        )
    else:
        async with database.sessions.begin() as db:
            messages = [
                message
                for _, message in await AgentRepository(db).read_history(
                    session_id, start_seq=0, through_seq=2**31 - 1
                )
            ]
            assert len(messages) == 1
            assert isinstance(messages[0], ModelRequest)
    async with database.sessions.begin() as db:
        assert (
            await db.execute(
                text("SELECT lock_token FROM agent_states WHERE session_id=:id"),
                {"id": session_id},
            )
        ).scalar_one() is None


@pytest.mark.parametrize("takeover_at", ["input_preparation", "cancel_boundary"])
async def test_runner_rejects_consumption_after_takeover(database, takeover_at):
    session_id, new_token = uuid4(), uuid4()
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
                    "UPDATE agent_states SET heartbeat_at=clock_timestamp() - interval '1 hour' "
                    "WHERE session_id=:id"
                ),
                {"id": session_id},
            )
            await AgentRepository(db).acquire(session_id, new_token, heartbeat_timeout=60)

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
            await sessions.consume_inputs(session_id, "steer", db=db, ids=[row.id for row in rows])

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
        await repo.lock_owned(session_id, new_token)
        messages = [
            message
            for _, message in await repo.read_history(
                session_id, start_seq=0, through_seq=2**31 - 1
            )
        ]
    assert len(messages) == (0 if takeover_at == "input_preparation" else 3)

"""Withdrawal races use real queue rows, SDK preparation and checkpoint transactions."""

import asyncio
from uuid import uuid4

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import text

from kapy.agent_runner.repository import AgentRepository
from kapy.control.sessions import SessionService
from kapy.control.sessions import service as session_service
from kapy.session_lease import open_session_lease
from kapy.session_lease import service as lease_service

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.mark.parametrize("withdraw_all", [False, True])
async def test_prepared_prompt_is_rebuilt_from_actual_consumption(
    database, seed_session, session_model, withdraw_all
):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    await seed_session(session_id)
    gone = await sessions.enqueue_input(session_id, "steer", "withdrawn")
    if not withdraw_all:
        await sessions.enqueue_input(session_id, "steer", "kept")
    prepared = []
    requests = []

    async def model(messages, info):
        requests.append(messages)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model))

    @agent.system_prompt(dynamic=True)
    async def prompt(ctx: RunContext) -> str:
        prepared.append(ctx.prompt)
        # This separate transaction also verifies SDK preparation holds no queue lock.
        await sessions.delete_input(session_id, gone.id)
        return f"Prepared for {ctx.prompt}"

    session_model(agent.model)
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished
    history = await sessions.read_history(session_id)
    if withdraw_all:
        assert requests == [] and history.items == []
        assert prepared == [["withdrawn"]]
    else:
        assert prepared == [["withdrawn", "kept"], ["kept"]]
        assert len(requests) == 1
        parts = history.items[0].message.parts
        assert [part.content for part in parts if isinstance(part, UserPromptPart)] == ["kept"]
        assert [part.content for part in parts if isinstance(part, SystemPromptPart)] == [
            "Prepared for ['kept']"
        ]
        assert requests[0][0].parts == history.items[0].message.parts
    assert await sessions.read_inputs(session_id, "steer") == ()


async def test_withdrawn_new_input_does_not_replace_a_pending_checkpoint(
    database, seed_history, seed_session, session_model, monkeypatch
):
    original = ModelRequest(parts=[UserPromptPart("already committed")])
    session_id = await seed_history([original], next_step="model_request")
    await seed_session(session_id)
    sessions = SessionService(database.sessions)
    gone = await sessions.enqueue_input(session_id, "steer", "withdrawn")
    read_inputs = sessions.read_inputs

    async def read_then_withdraw(session_id, channel):
        rows = await read_inputs(session_id, channel)
        await sessions.delete_input(session_id, gone.id)
        return rows

    monkeypatch.setattr(sessions, "read_inputs", read_then_withdraw)
    requests = []

    async def model(messages, info):
        requests.append(messages)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model))
    session_model(agent.model)
    await sessions.start_runner(session_id, agent=agent)
    assert len(requests) == 1 and len(requests[0]) == 1
    assert requests[0][0].parts == original.parts
    history = await sessions.read_history(session_id)
    assert [item.seq for item in history.items] == [0, 1]
    assert history.items[0].message == original


@pytest.mark.parametrize("rollback", [False, True])
async def test_delete_races_with_atomic_queue_to_history_commit(
    database, rollback, monkeypatch, wait_for_lock
):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    from kapy.control.sessions.repository import SessionRepository

    pid_ready = asyncio.get_running_loop().create_future()
    delete_input = SessionRepository.delete_input

    async def observe_delete(repo, session_id, input_id):
        pid_ready.set_result((await repo._db.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        return await delete_input(repo, session_id, input_id)

    monkeypatch.setattr(SessionRepository, "delete_input", observe_delete)
    async with database.sessions.begin() as db:
        row = await SessionRepository(db).enqueue_input(session_id, "queued", "candidate")
    deletion = None
    try:
        async with (
            open_session_lease(session_id, session_factory=database.sessions) as lease,
            database.sessions.begin() as db,
        ):
            await lease.lock_owned(db)
            repo = AgentRepository(db)
            await repo.resume(session_id)
            accepted = await sessions.consume_inputs(session_id, "queued", db=db, ids=[row.id])
            assert accepted == (row,)
            deletion = asyncio.create_task(sessions.delete_input(session_id, row.id))
            async with asyncio.timeout(5):
                pid = await pid_ready
            await wait_for_lock(pid)
            await repo.save_checkpoint(
                session_id,
                next_step="model_request",
                start_seq=0,
                messages=[ModelRequest(parts=[UserPromptPart(accepted[0].content)])],
            )
            if rollback:
                await db.rollback()
        async with asyncio.timeout(2):
            assert await deletion is rollback
    finally:
        if deletion is not None:
            deletion.cancel()
            await asyncio.gather(deletion, return_exceptions=True)
    history = await sessions.read_history(session_id)
    assert len(history.items) == (0 if rollback else 1)
    assert await sessions.read_inputs(session_id, "queued") == ()


@pytest.mark.parametrize("channel", ["queued", "steer"])
async def test_input_arriving_in_final_lease_release_window_is_drained(
    database, seed_session, session_model, monkeypatch, channel
):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    await seed_session(session_id)
    update_owned = lease_service._update_owned
    injected = False

    async def release_with_input(db, lease, *, release):
        session_id = lease.session_id
        nonlocal injected
        if release and not injected:
            injected = True
            await sessions.enqueue_input(session_id, channel, "during release")
            assert await sessions.is_runner_running(session_id)
        await update_owned(db, lease, release=release)

    monkeypatch.setattr(lease_service, "_update_owned", release_with_input)
    agent = Agent("test")
    session_model(agent.model)
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished and result.output is not None
    assert await sessions.read_inputs(session_id, channel) == ()
    assert not await sessions.is_runner_running(session_id)
    history = await sessions.read_history(session_id)
    assert any(
        isinstance(part, UserPromptPart) and part.content == "during release"
        for item in history.items
        for part in item.message.parts
    )


@pytest.mark.parametrize("completed_output", ["completed first", ""])
async def test_withdrawn_queued_snapshot_preserves_output_from_preceding_run(
    database, seed_session, session_model, monkeypatch, completed_output
):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    await seed_session(session_id)
    await sessions.enqueue_input(session_id, "steer", "first")
    queued = await sessions.enqueue_input(session_id, "queued", "withdrawn after snapshot")
    read_inputs = sessions.read_inputs
    withdrawn = False

    async def read_then_withdraw(session_id, channel):
        nonlocal withdrawn
        rows = await read_inputs(session_id, channel)
        if channel == "queued" and rows:
            withdrawn = await sessions.delete_input(session_id, queued.id)
        return rows

    monkeypatch.setattr(sessions, "read_inputs", read_then_withdraw)
    model_calls = 0

    async def model(messages, info):
        nonlocal model_calls
        model_calls += 1
        return ModelResponse(parts=[TextPart("completed first")])

    agent = Agent(FunctionModel(model))

    @agent.output_validator
    def output(value: str) -> str:
        return completed_output

    session_model(agent.model)
    result = await sessions.start_runner(session_id, agent=agent)
    assert withdrawn and result.finished and result.output == completed_output
    assert model_calls == 1
    assert len((await sessions.read_history(session_id)).items) == 2


@pytest.mark.parametrize("handoff", ["busy", "cancelled_unfinished"])
async def test_service_reacquisition_preserves_output_and_latest_finished_state(
    database, seed_session, session_model, monkeypatch, handoff
):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    await seed_session(session_id)
    await sessions.enqueue_input(session_id, "steer", "first")
    model_calls = 0

    async def model(messages, info):
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(parts=[TextPart("completed first")])
        return ModelResponse(parts=[ToolCallPart("pause", {}, "pause-call")])

    agent = Agent(FunctionModel(model))

    @agent.tool_plain
    async def pause() -> str:
        await sessions.request_cancel(session_id)
        return "paused before the next model request"

    run_agent_session = session_service.run_agent_session
    lower_calls = 0

    async def run_with_handoff(session_id, **kwargs):
        nonlocal lower_calls
        lower_calls += 1
        result = await run_agent_session(session_id, **kwargs)
        if lower_calls == 1:
            # The lower call has committed lease release. Input now forces a second
            # acquisition in SessionService, separate from the lower queued loop.
            await sessions.enqueue_input(session_id, "queued", "after release")
            if handoff == "busy":
                async with database.sessions.begin() as db:
                    await db.execute(
                        text(
                            "UPDATE session_leases SET lock_token=:token, "
                            "heartbeat_at=clock_timestamp() WHERE session_id=:id"
                        ),
                        {"id": session_id, "token": uuid4()},
                    )
        return result

    monkeypatch.setattr(session_service, "run_agent_session", run_with_handoff)
    session_model(agent.model)
    result = await sessions.start_runner(session_id, agent=agent)
    assert lower_calls == 2
    assert result.output == "completed first"
    assert result.finished is (handoff == "busy")

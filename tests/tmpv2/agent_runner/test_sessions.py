"""The user interaction service transfers exact snapshots into durable requests."""

from uuid import uuid4

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import text

from kapy.tmpv2.agent_runner.repository import AgentRepository
from kapy.tmpv2.control.sessions import SessionService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_exact_consumption_and_binary_roundtrip(database):
    sessions = SessionService(database.sessions)
    session_id, other_session_id = uuid4(), uuid4()
    assert await sessions.read_inputs(session_id, "steer") == ()
    first = await sessions.enqueue_input(
        session_id, "steer", ["binary\x00", BinaryContent(data=b"\x00\xff", media_type="image/png")]
    )
    snapshot = await sessions.read_inputs(session_id, "steer")
    second = await sessions.enqueue_input(session_id, "steer", "later")
    other = await sessions.enqueue_input(session_id, "queued", "queued")
    foreign = await sessions.enqueue_input(other_session_id, "steer", "another session")
    assert [item.id for item in snapshot] == [first.id]
    content = snapshot[0].content
    assert content[0] == "binary\x00"
    binary = content[1]
    assert isinstance(binary, BinaryContent)
    assert binary.data == b"\x00\xff"
    assert binary.media_type == "image/png"
    assert [item.id for item in await sessions.read_inputs(session_id, "steer")] == [
        first.id,
        second.id,
    ]
    async with database.sessions.begin() as db:
        await sessions.consume_inputs(
            session_id, "steer", db=db, ids=[first.id, other.id, foreign.id]
        )
    assert await sessions.read_inputs(session_id, "steer") == (second,)
    assert await sessions.read_inputs(session_id, "queued") == (other,)
    remaining = await sessions.read_inputs(other_session_id, "steer")
    assert [item.id for item in remaining] == [foreign.id]
    assert remaining[0].content == "another session"


async def test_cancel_coalesces_read_does_not_consume_and_rollback_restores(database):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    assert not await sessions.read_cancel(session_id)
    await sessions.request_cancel(session_id)
    await sessions.request_cancel(session_id)
    other_session_id = uuid4()
    await sessions.request_cancel(other_session_id)
    assert await sessions.read_cancel(session_id)
    with pytest.raises(RuntimeError):
        async with database.sessions.begin() as db:
            assert await sessions.consume_cancel(session_id, db=db)
            raise RuntimeError("rollback")
    assert await sessions.read_cancel(session_id)
    async with database.sessions.begin() as db:
        assert await sessions.consume_cancel(session_id, db=db)
        assert not await sessions.consume_cancel(session_id, db=db)
    assert not await sessions.read_cancel(session_id)
    assert await sessions.read_cancel(other_session_id)


async def test_steer_then_queued_then_cancel_preserves_finished_output(database):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    received = []

    async def model(messages, info):
        prompts = [
            part.content
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart)
        ]
        received.append(prompts)
        if len(received) == 1:
            await sessions.enqueue_input(session_id, "steer", "second steer")
        if len(received) == 3:
            await sessions.request_cancel(session_id)
        return ModelResponse(parts=[TextPart(str(len(received)))])

    await sessions.enqueue_input(session_id, "steer", "first steer")
    await sessions.enqueue_input(session_id, "queued", "queued")
    result = await sessions.start_runner(session_id, agent=Agent(FunctionModel(model)))
    assert result.finished and result.output == "3"
    assert received == [
        ["first steer"],
        ["first steer", "second steer"],
        ["first steer", "second steer", "queued"],
    ]
    assert not await sessions.read_cancel(session_id)
    assert not await sessions.read_inputs(session_id, "steer")
    assert not await sessions.read_inputs(session_id, "queued")


async def test_cancelled_unfinished_run_still_starts_queued_run(database):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    agent = Agent("test")

    @agent.tool_plain
    async def work() -> str:
        await sessions.request_cancel(session_id)
        await sessions.enqueue_input(session_id, "queued", "queued after cancel")
        return "ok"

    await sessions.enqueue_input(session_id, "steer", "go")
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished
    assert not await sessions.read_inputs(session_id, "queued")
    async with database.sessions.begin() as db:
        messages = await AgentRepository(db).read_history(session_id)
    assert any(
        isinstance(part, UserPromptPart) and part.content == "queued after cancel"
        for message in messages
        for part in message.parts
    )


async def test_first_dynamic_system_prompt_prepared_outside_transaction(database):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    agent = Agent("test", system_prompt="static")

    @agent.system_prompt(dynamic=True)
    async def dynamic() -> str:
        # A separate DB write proves no long-lived runner transaction is holding
        # the state row while user-provided prompt preparation waits.
        async with database.sessions.begin() as db:
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            await db.execute(
                text("UPDATE agent_states SET updated_at=updated_at WHERE session_id=:id"),
                {"id": session_id},
            )
        return "dynamic"

    await sessions.enqueue_input(session_id, "steer", "one")
    await sessions.enqueue_input(session_id, "steer", "two")
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished
    async with database.sessions.begin() as db:
        messages = await AgentRepository(db).read_history(session_id)
    assert [
        part.content
        for part in messages[0].parts
        if isinstance(part, (SystemPromptPart, UserPromptPart))
    ] == ["static", "dynamic", "one", "two"]

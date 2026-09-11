"""Observable recovery checkpoints exercised with real DB transactions and SDK nodes."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kapy.tmpv2.agent_runner import InputBatch, open_runner
from kapy.tmpv2.agent_runner.repository import AgentRepository
from kapy.tmpv2.control.sessions import SessionService
from kapy.tmpv2.control.sessions.repository import SessionRepository

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def history(database, session_id):
    async with database.sessions.begin() as db:
        return [
            message
            for _, message in await AgentRepository(db).read_history(
                session_id, start_seq=0, through_seq=2**31 - 1
            )
        ]


async def seed(database, session_id, next_step, messages):
    token = uuid4()
    async with database.sessions.begin() as db:
        repo = AgentRepository(db)
        await repo.acquire(session_id, token, heartbeat_timeout=60)
        await repo.lock_owned(session_id, token)
        await repo.save_checkpoint(session_id, next_step=next_step, start_seq=0, messages=messages)
        await repo.release(session_id, token)


async def test_text_done_continue_and_owned_resources(database):
    session_id = uuid4()
    agent = Agent(TestModel(custom_output_text="hello"), system_prompt="system")
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert (await runner.turn()).finished
        assert await history(database, session_id) == []
        first = await runner.turn(steer=["one", "two"])
        assert first.finished and first.output == "hello"
        before = deepcopy(await history(database, session_id))
        assert [p.content for p in before[0].parts if isinstance(p, UserPromptPart)] == [
            "one",
            "two",
        ]
        assert (await runner.turn()).output is None
        assert await history(database, session_id) == before
        await runner.turn(steer=["three"])
        after = await history(database, session_id)
        assert after[:2] == before
        assert len(after) == 4
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert runner.next_step == "done"
        assert (await runner.turn()).output is None
    async with database.sessions.begin() as db:
        assert (await db.execute(text("select lock_token from agent_states"))).scalar_one() is None
        rows = (
            await db.execute(text("select seq, message_metadata from agent_history order by seq"))
        ).all()
        assert [row.seq for row in rows] == list(range(4))
        assert all("usage" not in row.message_metadata for row in rows)


async def test_tools_checkpoint_resume_without_reexecuting_tools(
    database, toolset_lifecycle, heartbeat_observation
):
    called = []
    heartbeat_tasks, heartbeat_called = heartbeat_observation
    agent = Agent(TestModel(), toolsets=[toolset_lifecycle])

    @agent.tool_plain
    def work() -> str:
        called.append("tool")
        return "worked"

    session_id = uuid4()
    async with open_runner(
        session_id, agent=agent, session_factory=database.sessions, heartbeat_interval=0.01
    ) as runner:
        await runner.rebuild_context()
        assert not (await runner.turn(steer=["go"])).finished
        await asyncio.wait_for(heartbeat_called.wait(), 5)
        assert toolset_lifecycle.events == ["enter"]
        assert runner.next_step == "model_request"
        saved = await history(database, session_id)
        assert [m.kind for m in saved] == ["request", "response", "request"]
    assert called == ["tool"]
    assert toolset_lifecycle.events == ["enter", "exit"]
    assert heartbeat_tasks and all(task.done() for task in heartbeat_tasks)
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        result = await runner.turn()
        assert result.finished
    assert called == ["tool"]
    assert (await history(database, session_id))[:3] == saved
    assert toolset_lifecycle.events == ["enter", "exit", "enter", "exit"]


async def test_resume_handle_response_calls_no_model(database):
    model_calls = []
    tool_calls = []

    def model(messages, info):
        model_calls.append(messages)
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model), instructions="always present")

    @agent.tool_plain
    def work() -> str:
        tool_calls.append(1)
        return "ok"

    session_id = uuid4()
    await seed(
        database,
        session_id,
        "handle_response",
        [
            ModelRequest(
                parts=[UserPromptPart("go"), UserPromptPart("more")],
                metadata={"source": "human", "nested": {"ok": True}},
            ),
            ModelResponse(
                parts=[TextPart("checking"), ToolCallPart("work", {}, "call-1")],
                metadata={"source": "provider"},
                finish_reason="tool_call",
                provider_response_id="response-123",
            ),
        ],
    )
    restored = await history(database, session_id)
    assert restored[0].metadata == {"source": "human", "nested": {"ok": True}}
    assert [part.content for part in restored[0].parts if isinstance(part, UserPromptPart)] == [
        "go",
        "more",
    ]
    response = restored[1]
    assert isinstance(response, ModelResponse)
    assert response.metadata == {"source": "provider"}
    assert response.finish_reason == "tool_call"
    assert response.provider_response_id == "response-123"
    assert [part.part_kind for part in response.parts] == ["text", "tool-call"]
    assert isinstance(response.parts[0], TextPart) and response.parts[0].content == "checking"
    assert isinstance(response.parts[1], ToolCallPart)
    assert response.parts[1].tool_call_id == "call-1"
    async with database.sessions.begin() as db:
        row = (
            await db.execute(
                text(
                    "SELECT kind, message, message_metadata, finish_reason FROM agent_history "
                    "WHERE session_id=:id AND seq=1"
                ),
                {"id": session_id},
            )
        ).one()
        assert row.kind == "response"
        assert set(row.message) == {"parts"}
        assert "parts" not in row.message_metadata and "usage" not in row.message_metadata
        assert "finish_reason" not in row.message_metadata
        assert row.finish_reason == "tool_call"
        assert row.message_metadata["provider_response_id"] == "response-123"
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        result = await runner.turn()
        assert not result.finished
        assert not model_calls
    assert tool_calls == [1]
    messages = await history(database, session_id)
    part = messages[-1].parts[0]
    assert isinstance(part, ToolReturnPart)
    assert part.tool_call_id == "call-1"


async def test_recovery_text_with_instructions_does_not_request_model(database):
    def unexpected(messages, info):
        raise AssertionError("saved final response must not be requested again")

    agent = Agent(FunctionModel(unexpected), instructions="instructions")
    session_id = uuid4()
    await seed(
        database,
        session_id,
        "handle_response",
        [
            ModelRequest(parts=[UserPromptPart("go")]),
            ModelResponse(parts=[TextPart("saved")]),
        ],
    )
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        result = await runner.turn()
        assert result.finished and result.output == "saved"
    assert len(await history(database, session_id)) == 2


async def test_output_tool_and_retry_are_append_only(database):
    class Output(BaseModel):
        value: int

    agent = Agent(TestModel(custom_output_args={"value": 7}), output_type=Output)
    attempts = 0

    @agent.output_validator
    def validate(output: Output) -> Output:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelRetry("try again")
        return output

    session_id = uuid4()
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert not (await runner.turn(steer=["go"])).finished
        saved = await history(database, session_id)
        assert saved[-1].parts[0].part_kind == "retry-prompt"
        result = await runner.turn()
        assert result.finished and result.output == Output(value=7)
    messages = await history(database, session_id)
    assert messages[: len(saved)] == saved
    assert messages[-1].parts[0].part_kind == "tool-return"


async def test_tool_failure_preserves_response_and_replays_batch(
    database, toolset_lifecycle, heartbeat_observation
):
    calls = []
    heartbeat_tasks, heartbeat_called = heartbeat_observation
    should_fail = True
    agent = Agent(TestModel(), toolsets=[toolset_lifecycle])

    @agent.tool_plain(sequential=True)
    async def first() -> str:
        calls.append("first")
        await asyncio.wait_for(heartbeat_called.wait(), 5)
        return "ok"

    @agent.tool_plain(sequential=True)
    def second() -> str:
        calls.append("second")
        if should_fail:
            raise RuntimeError("tool failed")
        return "ok"

    session_id = uuid4()
    with pytest.raises(Exception, match="tool failed"):
        async with open_runner(
            session_id, agent=agent, session_factory=database.sessions, heartbeat_interval=0.01
        ) as runner:
            await runner.rebuild_context()
            await runner.turn(steer=["go"])
    assert toolset_lifecycle.events == ["enter", "exit"]
    assert heartbeat_tasks and all(task.done() for task in heartbeat_tasks)
    saved = await history(database, session_id)
    assert [m.kind for m in saved] == ["request", "response"]
    should_fail = False
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert runner.next_step == "handle_response"
        assert not (await runner.turn()).finished
    assert calls == ["first", "second", "first", "second"]
    assert (await history(database, session_id))[:2] == saved


async def test_input_rollback_after_consumption(database):
    session_id = uuid4()
    async with database.sessions.begin() as db:
        item = await SessionRepository(db).enqueue_input(session_id, "steer", "keep")

    async def consume(db):
        await SessionRepository(db).consume_inputs(session_id, "steer", ids=[item.id])
        raise RuntimeError("after delete")

    async def read():
        return InputBatch(("keep",), consume)

    async def no_cancel(db):
        return False

    with pytest.raises(RuntimeError, match="after delete"):
        async with open_runner(
            session_id, agent=Agent(TestModel()), session_factory=database.sessions
        ) as runner:
            await runner.run(read_steer=read, consume_cancel=no_cancel)
    assert await history(database, session_id) == []
    async with database.sessions.begin() as db:
        assert len(await SessionRepository(db).read_inputs(session_id, "steer")) == 1


async def test_model_cancel_recovers_accepted_request_and_closes_native(
    database, toolset_lifecycle, heartbeat_observation
):
    entered = asyncio.Event()
    heartbeat_tasks, heartbeat_called = heartbeat_observation

    async def model(messages, info):
        entered.set()
        await asyncio.Event().wait()
        return ModelResponse(parts=[])

    session_id = uuid4()

    async def execute():
        async with open_runner(
            session_id,
            agent=Agent(FunctionModel(model), toolsets=[toolset_lifecycle]),
            session_factory=database.sessions,
            heartbeat_interval=0.01,
        ) as runner:
            await runner.rebuild_context()
            await runner.turn(steer=["once"])

    task = asyncio.create_task(execute())
    await asyncio.wait_for(entered.wait(), 5)
    await asyncio.wait_for(heartbeat_called.wait(), 5)
    assert toolset_lifecycle.events == ["enter"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert toolset_lifecycle.events == ["enter", "exit"]
    assert heartbeat_tasks and all(task.done() for task in heartbeat_tasks)
    saved = await history(database, session_id)
    assert len(saved) == 1
    async with open_runner(
        session_id, agent=Agent(TestModel()), session_factory=database.sessions
    ) as runner:
        await runner.rebuild_context()
        assert runner.next_step == "model_request"
        assert (await runner.turn()).finished
    assert (await history(database, session_id))[0] == saved[0]


async def test_cross_task_and_reentrant_calls_rejected(database):
    session_id = uuid4()
    agent = Agent(TestModel())
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        with pytest.raises(RuntimeError, match="task that opened"):
            await asyncio.create_task(runner.turn())

        async def read():
            with pytest.raises(RuntimeError, match="already executing"):
                await runner.turn()
            return None

        async def no_cancel(db):
            return False

        assert (await runner.run(read_steer=read, consume_cancel=no_cancel)).finished


@pytest.mark.parametrize("failure_at", ["prepare", "before_commit", "after_commit"])
async def test_input_acceptance_fault_preserves_atomic_recoverable_state(database, failure_at):
    session_id = uuid4()
    sessions = SessionService(database.sessions)
    await sessions.enqueue_input(session_id, "steer", "accept once")

    class InjectedFailure(RuntimeError):
        pass

    class FailingSessions(async_sessionmaker[AsyncSession]):
        @asynccontextmanager
        # Preserve begin's context-manager contract while injecting around the
        # real transaction exit; SQLAlchemy annotates its private concrete type.
        async def begin(self):  # pyrefly: ignore[bad-override]
            accepted = False
            async with super().begin() as db:
                yield db
                if db.info.get("accepting_input"):
                    # Observe all three real SQL effects before injecting at the
                    # transaction boundary, rather than replacing save_checkpoint.
                    accepted = True
                    assert (
                        await db.execute(
                            text("SELECT count(*) FROM session_inputs WHERE session_id=:id"),
                            {"id": session_id},
                        )
                    ).scalar_one() == 0
                    assert (
                        await db.execute(
                            text("SELECT count(*) FROM agent_history WHERE session_id=:id"),
                            {"id": session_id},
                        )
                    ).scalar_one() == 1
                    assert (
                        await db.execute(
                            text("SELECT next_step FROM agent_states WHERE session_id=:id"),
                            {"id": session_id},
                        )
                    ).scalar_one() == "model_request"
                    if failure_at == "before_commit":
                        raise InjectedFailure("before_commit")
            if accepted and failure_at == "after_commit":
                raise InjectedFailure("after_commit")

    agent = Agent(TestModel(custom_output_text="ok"))
    fail_preparation = failure_at == "prepare"

    @agent.system_prompt(dynamic=True)
    async def dynamic() -> str:
        if fail_preparation:
            raise InjectedFailure("prepare")
        return "system"

    async def read_steer():
        rows = await sessions.read_inputs(session_id, "steer")
        if not rows:
            return None

        async def consume(db):
            await sessions.consume_inputs(session_id, "steer", db=db, ids=[row.id for row in rows])
            db.info["accepting_input"] = True

        return InputBatch(tuple(row.content for row in rows), consume)

    async def consume_cancel(db):
        return await sessions.consume_cancel(session_id, db=db)

    with pytest.raises(InjectedFailure, match=failure_at):
        async with open_runner(
            session_id,
            agent=agent,
            session_factory=FailingSessions(database.engine, expire_on_commit=False),
        ) as runner:
            await runner.rebuild_context()
            with pytest.raises(InjectedFailure, match=failure_at):
                await runner.run(read_steer=read_steer, consume_cancel=consume_cancel)
            # Even when the caller catches the error, the old handle must not retry
            # a commit whose outcome may already have been accepted by PostgreSQL.
            with pytest.raises(InjectedFailure):
                await runner.turn(steer=["must not be accepted"])

    committed = failure_at == "after_commit"
    assert len(await sessions.read_inputs(session_id, "steer")) == (0 if committed else 1)
    saved = await history(database, session_id)
    assert len(saved) == (1 if committed else 0)
    async with database.sessions.begin() as db:
        assert (
            await db.execute(
                text("SELECT next_step FROM agent_states WHERE session_id=:id"), {"id": session_id}
            )
        ).scalar_one() == ("model_request" if committed else "done")
    fail_preparation = False
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished and result.output == "ok"
    assert not await sessions.read_inputs(session_id, "steer")
    restored = await history(database, session_id)
    assert [
        part.content
        for message in restored
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ] == ["accept once"]
    async with database.sessions.begin() as db:
        seqs = (
            (
                await db.execute(
                    text("SELECT seq FROM agent_history WHERE session_id=:id ORDER BY seq"),
                    {"id": session_id},
                )
            )
            .scalars()
            .all()
        )
    assert seqs == list(range(len(restored)))


async def test_initial_and_steer_wait_for_saved_response_then_accept_exact_snapshot(database):
    session_id = uuid4()
    sessions = SessionService(database.sessions)
    entered, finish = asyncio.Event(), asyncio.Event()
    model_prompts = []

    def model(messages, info):
        model_prompts.append(
            [
                part.content
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart)
            ]
        )
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model))

    @agent.tool_plain
    async def work() -> str:
        entered.set()
        await finish.wait()
        return "result"

    await seed(
        database,
        session_id,
        "handle_response",
        [
            ModelRequest(parts=[UserPromptPart("original")]),
            ModelResponse(parts=[ToolCallPart("work", {}, "saved-call")]),
        ],
    )
    first = await sessions.enqueue_input(session_id, "queued", "queued first")
    second = await sessions.enqueue_input(session_id, "queued", "queued second")
    await sessions.enqueue_input(session_id, "steer", "steer first")
    await sessions.enqueue_input(session_id, "steer", "steer second")
    snapshot = await sessions.read_inputs(session_id, "queued")
    consumed = []
    steer_reads = []

    async def consume_initial(db):
        consumed.append("queued")
        await sessions.consume_inputs(session_id, "queued", db=db, ids=[row.id for row in snapshot])

    initial = InputBatch(tuple(row.content for row in snapshot), consume_initial)

    async def read_steer():
        steer_reads.append(True)
        rows = await sessions.read_inputs(session_id, "steer")
        if not rows:
            return None

        async def consume(db):
            consumed.append("steer")
            await sessions.consume_inputs(session_id, "steer", db=db, ids=[row.id for row in rows])

        return InputBatch(tuple(row.content for row in rows), consume)

    async def consume_cancel(db):
        return await sessions.consume_cancel(session_id, db=db)

    async def producer():
        await entered.wait()
        extra = await sessions.enqueue_input(session_id, "queued", "outside snapshot")
        await sessions.request_cancel(session_id)
        finish.set()
        return extra

    producer_task = asyncio.create_task(producer())
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        result = await runner.run(
            initial=initial, read_steer=read_steer, consume_cancel=consume_cancel
        )
        extra = await producer_task
        assert not result.finished and runner.next_step == "model_request"
        assert consumed == [] and steer_reads == [] and model_prompts == []
        assert [row.id for row in await sessions.read_inputs(session_id, "queued")] == [
            first.id,
            second.id,
            extra.id,
        ]
        assert len(await sessions.read_inputs(session_id, "steer")) == 2
        result = await runner.run(
            initial=initial, read_steer=read_steer, consume_cancel=consume_cancel
        )
        assert result.finished and result.output == "done"
    assert model_prompts == [
        ["original", "queued first", "queued second", "steer first", "steer second"]
    ]
    messages = await history(database, session_id)
    accepted = messages[3]
    assert isinstance(accepted, ModelRequest)
    assert [part.content for part in accepted.parts if isinstance(part, UserPromptPart)] == [
        "queued first",
        "queued second",
        "steer first",
        "steer second",
    ]
    assert [row.id for row in await sessions.read_inputs(session_id, "queued")] == [extra.id]
    assert not await sessions.read_inputs(session_id, "steer")

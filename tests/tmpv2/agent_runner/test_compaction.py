"""Summaries are fenced side records; reconstructed contexts resume real SDK graphs."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.concurrency import ConcurrencyLimitedModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from kapy.tmpv2.agent_runner import RunnerLost, open_runner
from kapy.tmpv2.agent_runner.compaction import (
    COMPACTION_CONTEXT_PROMPT,
    COMPACTION_PROMPT,
    COMPACTION_RESUME_PROMPT,
)
from kapy.tmpv2.agent_runner.repository import AgentRepository
from kapy.tmpv2.control.sessions import SessionService, UpdateSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def user_texts(messages):
    return [
        part.content
        for message in messages
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]


async def snapshot(database, session_id):
    async with database.sessions.begin() as db:
        repo = AgentRepository(db)
        rows = await repo.read_history(session_id, start_seq=0, through_seq=2**31 - 1)
        return rows, await repo.read_latest_compaction(session_id)


@pytest.mark.parametrize("apply_summary,reopen", [(False, False), (True, False), (True, True)])
async def test_manual_summary_preserves_pending_graph_history_and_absolute_sequences(
    database, apply_summary, reopen
):
    business, summaries, tools = [], [], []

    def model(messages, info):
        if COMPACTION_PROMPT in user_texts(messages):
            summaries.append(deepcopy(messages))
            return ModelResponse(parts=[TextPart("saved summary")])
        business.append(deepcopy(messages))
        if len(business) == 1:
            return ModelResponse(parts=[ToolCallPart("work", {}, "call-id")])
        return ModelResponse(parts=[TextPart("business output")])

    # Request-level limits must permit summary calls while the main graph is paused.
    agent = Agent(ConcurrencyLimitedModel(FunctionModel(model), limiter=1), system_prompt="system")

    @agent.tool_plain
    def work() -> str:
        tools.append(True)
        return "tool result"

    session_id = uuid4()
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert not (await runner.turn(steer=["original"])).finished
        before, _ = await snapshot(database, session_id)
        summary = await runner.compact()
        assert summary is not None and summary.last_message_seq == 2
        assert runner.next_step == "model_request"
        assert (await snapshot(database, session_id))[0] == before
        assert await runner.compact() == summary
        assert len(summaries) == 1 and tools == [True]
        if not reopen:
            if apply_summary:
                await runner.rebuild_context(compaction_replay_turns=0)
            result = await runner.turn(steer=["new steer"])
            assert result.finished and result.output == "business output"
    if reopen:
        async with open_runner(
            session_id, agent=agent, session_factory=database.sessions
        ) as runner:
            await runner.rebuild_context(compaction_replay_turns=0)
            assert await runner.compact() == summary
            result = await runner.turn(steer=["new steer"])
            assert result.finished and result.output == "business output"
    prompts = user_texts(business[-1])
    assert prompts == (
        [
            COMPACTION_CONTEXT_PROMPT.format(summary_text="saved summary"),
            COMPACTION_RESUME_PROMPT,
            "new steer",
        ]
        if apply_summary
        else ["original", "new steer"]
    )
    assert [
        p.content for m in business[-1] for p in m.parts if isinstance(p, SystemPromptPart)
    ] == ["system"]
    after, saved = await snapshot(database, session_id)
    assert after[: len(before)] == before
    assert [seq for seq, _ in after] == list(range(5))
    assert saved == summary and len(summaries) == 1 and tools == [True]


@pytest.mark.parametrize("next_step", ["model_request", "handle_response"])
async def test_rebuild_reads_only_anchor_window_and_tail_and_preserves_resume_boundary(
    database, seed_history, monkeypatch, next_step
):
    messages = []
    for index in range(100):
        messages.extend(
            [
                ModelRequest(parts=[UserPromptPart(f"question {index}")]),
                ModelResponse(parts=[TextPart(f"answer {index}")]),
            ]
        )
    messages[0] = ModelRequest(
        parts=[SystemPromptPart("persisted system"), UserPromptPart("question 0")]
    )
    messages[199:] = [
        ModelResponse(
            parts=[
                TextPart("using tools"),
                ToolCallPart("work", {}, "a-id"),
                ToolCallPart("work", {}, "b-id"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart("work", "first result", "a-id"),
                RetryPromptPart("retry", tool_name="work", tool_call_id="b-id"),
            ]
        ),
        ModelResponse(parts=[ToolCallPart("work", {}, "b2-id")]),
        ModelRequest(parts=[ToolReturnPart("work", "second result", "b2-id")]),
    ]
    replay_tool_parts = [
        deepcopy(part)
        for message in messages[198:]
        for part in message.parts
        if isinstance(part, (ToolCallPart, ToolReturnPart, RetryPromptPart))
    ]
    messages.append(ModelRequest(parts=[UserPromptPart("pending")]))
    if next_step == "handle_response":
        messages.append(ModelResponse(parts=[ToolCallPart("work", {}, "pending-call")]))
    session_id = await seed_history(messages, next_step, compaction_seq=202)
    reads = []
    original_read = AgentRepository.read_history
    original_before = AgentRepository.read_history_before

    async def read(self, session_id, **kwargs):
        result = await original_read(self, session_id, **kwargs)
        reads.extend(seq for seq, _ in result)
        return result

    async def before(self, session_id, *, through_seq, limit):
        result = await original_before(self, session_id, through_seq=through_seq, limit=3)
        reads.extend(seq for seq, _ in result)
        return result

    monkeypatch.setattr(AgentRepository, "read_history", read)
    monkeypatch.setattr(AgentRepository, "read_history_before", before)
    received, tools = [], []

    def model(messages, info):
        received.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("final")])

    agent = Agent(FunctionModel(model))

    @agent.tool_plain
    def work() -> str:
        tools.append(True)
        return "ok"

    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        assert reads == []
        await runner.rebuild_context(compaction_replay_turns=1)
        assert all(seq == 0 or seq >= 197 for seq in reads)
        assert len(reads) < 12
        loaded = list(reads)
        result = await runner.turn()
        if next_step == "handle_response":
            assert not result.finished and received == [] and tools == [True]
            # Rebuilding an already-open pending request must not append it twice.
            await runner.rebuild_context(compaction_replay_turns=1)
            loaded = list(reads)
            result = await runner.turn()
        assert result.finished
        assert reads == loaded
    assert user_texts(received[0]) == [
        COMPACTION_CONTEXT_PROMPT.format(summary_text="saved summary"),
        "question 99",
        COMPACTION_RESUME_PROMPT,
        "pending",
    ]
    assert [p.content for m in received[0] for p in m.parts if isinstance(p, SystemPromptPart)] == [
        "persisted system"
    ]
    assert [
        part
        for message in received[0]
        for part in message.parts
        if isinstance(part, (ToolCallPart, ToolReturnPart, RetryPromptPart))
        and part.tool_call_id != "pending-call"
    ] == replay_tool_parts
    assert tools == ([True] if next_step == "handle_response" else [])
    if next_step == "handle_response":
        assert [
            p.tool_call_id for m in received[0] for p in m.parts if isinstance(p, ToolReturnPart)
        ] == ["a-id", "b2-id", "pending-call"]


async def test_threshold_persists_normalized_usage_and_restarts_without_recompacting_anchor(
    database, seed_session, session_model
):
    session_id = uuid4()
    await seed_session(session_id)
    sessions = SessionService(database.sessions)
    calls = []
    summaries = 0

    async def model(messages, info):
        nonlocal summaries
        summary = COMPACTION_PROMPT in user_texts(messages)
        calls.append((summary, deepcopy(messages)))
        if summary:
            summaries += 1
            await sessions.enqueue_input(session_id, "queued", f"queued after summary {summaries}")
            return ModelResponse(
                parts=[TextPart(f"summary {summaries}")],
                usage=RequestUsage(input_tokens=9999, output_tokens=1000),
            )
        if len(calls) == 1:
            return ModelResponse(
                parts=[TextPart("first output")],
                usage=RequestUsage(input_tokens=100, output_tokens=20, cache_read_tokens=80),
            )
        if len(calls) == 3:
            return ModelResponse(
                parts=[TextPart("second output")],
                usage=RequestUsage(input_tokens=130, output_tokens=20),
            )
        return ModelResponse(
            parts=[TextPart("queued output")], usage=RequestUsage(input_tokens=10, output_tokens=0)
        )

    await sessions.enqueue_input(session_id, "steer", "start")
    agent = Agent(FunctionModel(model))
    await sessions.update_session(
        session_id, UpdateSession(compaction_threshold_tokens=119, compaction_replay_turns=0)
    )
    session_model(agent.model)
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished and result.output == "queued output"
    assert [summary for summary, _ in calls] == [False, True, False, True, False]
    assert user_texts(calls[-1][1]) == [
        COMPACTION_CONTEXT_PROMPT.format(summary_text="summary 2"),
        COMPACTION_RESUME_PROMPT,
        "queued after summary 2",
    ]
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished and result.output is None
    assert len(calls) == 5
    await sessions.enqueue_input(session_id, "steer", "after restart")
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished and result.output == "queued output"
    assert len(calls) == 6 and summaries == 2
    assert user_texts(calls[-1][1]) == [
        COMPACTION_CONTEXT_PROMPT.format(summary_text="summary 2"),
        COMPACTION_RESUME_PROMPT,
        "queued after summary 2",
        "after restart",
    ]
    rows, compaction = await snapshot(database, session_id)
    assert compaction is not None and compaction.last_message_seq == 3
    assert compaction.text == "summary 2"
    assert [seq for seq, _ in rows] == list(range(8))
    async with database.sessions.begin() as db:
        stored = (
            await db.execute(
                text(
                    "SELECT input_tokens, output_tokens, message_metadata "
                    "FROM agent_history ORDER BY seq"
                )
            )
        ).all()
    assert [(row.input_tokens, row.output_tokens) for row in stored] == [
        (None, None),
        (100, 20),
        (None, None),
        (130, 20),
        (None, None),
        (10, 0),
        (None, None),
        (10, 0),
    ]
    assert all("usage" not in row.message_metadata for row in stored)


@pytest.mark.parametrize(
    "tokens,threshold,expected",
    [
        ((100, 20), 120, False),
        ((100, 20), 119, True),
        ((0, 0), 1, False),
        ((0, 20), 1, False),
        ((100, 0), 99, True),
    ],
)
async def test_threshold_restores_latest_response_observation(
    database, seed_history, tokens, threshold, expected, seed_session, session_model
):
    session_id = await seed_history(
        [
            ModelRequest(parts=[UserPromptPart("go")]),
            ModelResponse(
                parts=[TextPart("done")],
                usage=RequestUsage(input_tokens=tokens[0], output_tokens=tokens[1]),
            ),
        ]
    )
    await seed_session(session_id)
    calls = []

    def model(messages, info):
        calls.append(True)
        return ModelResponse(parts=[TextPart("summary")])

    sessions = SessionService(database.sessions)
    await sessions.update_session(session_id, UpdateSession(compaction_threshold_tokens=threshold))
    session_model(Agent(FunctionModel(model)).model)
    result = await sessions.start_runner(session_id, agent=Agent(FunctionModel(model)))
    assert result.finished and result.output is None
    assert len(calls) == int(expected)
    _, compaction = await snapshot(database, session_id)
    assert (compaction is not None) is expected


@pytest.mark.parametrize("reopen", [False, True])
async def test_latest_unknown_usage_supersedes_older_high_usage(
    database, seed_history, reopen, seed_session, session_model
):
    session_id = await seed_history(
        [
            ModelRequest(parts=[UserPromptPart("old")]),
            ModelResponse(
                parts=[TextPart("old output")],
                usage=RequestUsage(input_tokens=100, output_tokens=20),
            ),
        ]
    )
    await seed_session(session_id)
    calls = []

    def model(messages, info):
        calls.append(deepcopy(messages))
        # A nonzero output prevents FunctionModel from estimating absent usage;
        # absent input still makes this response's context observation unknown.
        return ModelResponse(
            parts=[TextPart("latest output")],
            usage=RequestUsage(input_tokens=0, output_tokens=20),
        )

    async def no_steer():
        return None

    async def no_cancel(db):
        return False

    agent = Agent(FunctionModel(model))
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert (await runner.turn(steer=["new"])).finished
        if not reopen:
            assert (
                await runner.run(
                    read_steer=no_steer, consume_cancel=no_cancel, compaction_threshold_tokens=10
                )
            ).finished
    if reopen:
        await SessionService(database.sessions).update_session(
            session_id, UpdateSession(compaction_threshold_tokens=10)
        )
        session_model(agent.model)
        result = await SessionService(database.sessions).start_runner(session_id, agent=agent)
        assert result.finished
    assert len(calls) == 1
    rows, compaction = await snapshot(database, session_id)
    assert len(rows) == 4 and compaction is None


@pytest.mark.parametrize("operation", ["turn", "compact"])
async def test_manual_execution_requires_prepared_context(database, operation):
    with pytest.raises(RuntimeError, match="context has not been prepared"):
        async with open_runner(
            uuid4(), agent=Agent("test"), session_factory=database.sessions
        ) as runner:
            await getattr(runner, operation)()


async def test_empty_compaction_and_handle_response_rejection(database, seed_history):
    def unexpected(messages, info):
        raise AssertionError("no summary model request is allowed")

    agent = Agent(FunctionModel(unexpected))
    async with open_runner(uuid4(), agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert await runner.compact() is None
    session_id = await seed_history(
        [ModelRequest(parts=[UserPromptPart("go")]), ModelResponse(parts=[TextPart("saved")])],
        "handle_response",
    )
    with pytest.raises(ValueError, match="handling the saved response"):
        async with open_runner(
            session_id, agent=agent, session_factory=database.sessions
        ) as runner:
            await runner.rebuild_context()
            await runner.compact()


async def test_summary_wait_releases_db_and_takeover_fences_insert(database, seed_history):
    session_id = await seed_history(
        [ModelRequest(parts=[UserPromptPart("go")]), ModelResponse(parts=[TextPart("done")])]
    )
    entered, finish = asyncio.Event(), asyncio.Event()
    replacement = uuid4()

    async def model(messages, info):
        entered.set()
        await finish.wait()
        return ModelResponse(parts=[TextPart("stale summary")])

    async def execute():
        async with open_runner(
            session_id, agent=Agent(FunctionModel(model)), session_factory=database.sessions
        ) as runner:
            await runner.rebuild_context()
            await runner.compact()

    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        async with database.sessions.begin() as db:
            await db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            await db.execute(
                text(
                    "UPDATE agent_states SET heartbeat_at=clock_timestamp() - interval '1 hour' "
                    "WHERE session_id=:id"
                ),
                {"id": session_id},
            )
            await AgentRepository(db).acquire(session_id, replacement, heartbeat_timeout=60)
        finish.set()
        with pytest.raises(RunnerLost):
            await asyncio.wait_for(task, 5)
    finally:
        finish.set()
        await asyncio.gather(task, return_exceptions=True)
    async with database.sessions.begin() as db:
        await AgentRepository(db).lock_owned(session_id, replacement)
    rows, summary = await snapshot(database, session_id)
    assert len(rows) == 2 and summary is None


@pytest.mark.parametrize("after_commit", [False, True])
async def test_summary_commit_failure_recovers_from_saved_anchor(
    database, seed_history, monkeypatch, after_commit
):
    session_id = await seed_history(
        [ModelRequest(parts=[UserPromptPart("go")]), ModelResponse(parts=[TextPart("done")])]
    )
    original_save = AgentRepository.save_compaction

    async def mark(self, *args, **kwargs):
        result = await original_save(self, *args, **kwargs)
        self._db.info["summary_saved"] = True
        return result

    monkeypatch.setattr(AgentRepository, "save_compaction", mark)

    class FailingSessions(async_sessionmaker):
        @asynccontextmanager
        async def begin(self):  # pyrefly: ignore[bad-override]
            marked = False
            async with super().begin() as db:
                yield db
                marked = db.info.get("summary_saved", False)
                if marked and not after_commit:
                    raise RuntimeError("summary commit failed")
            if marked and after_commit:
                raise RuntimeError("summary commit failed")

    calls = []

    def model(messages, info):
        calls.append(True)
        return ModelResponse(parts=[TextPart("summary")])

    agent = Agent(FunctionModel(model))
    with pytest.raises(RuntimeError, match="summary commit failed"):
        async with open_runner(
            session_id,
            agent=agent,
            session_factory=FailingSessions(database.engine, expire_on_commit=False),
        ) as runner:
            await runner.rebuild_context()
            await runner.compact()
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        result = await runner.compact()
    assert result is not None and result.last_message_seq == 1
    assert len(calls) == (1 if after_commit else 2)
    assert len((await snapshot(database, session_id))[0]) == 2


async def test_automatic_summary_finishes_saved_tools_before_compacting_and_honors_cancel(
    database, seed_history, seed_session, session_model
):
    session_id = await seed_history(
        [
            ModelRequest(parts=[UserPromptPart("go")]),
            ModelResponse(
                parts=[ToolCallPart("work", {}, "saved-call")],
                usage=RequestUsage(input_tokens=100, output_tokens=20),
            ),
        ],
        "handle_response",
    )
    await seed_session(session_id)
    sessions = SessionService(database.sessions)
    events = []

    async def model(messages, info):
        if COMPACTION_PROMPT in user_texts(messages):
            events.append("summary")
            assert events == ["tool", "summary"]
            assert any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts)
            await sessions.request_cancel(session_id)
            return ModelResponse(parts=[TextPart("summary")])
        events.append("business")
        return ModelResponse(
            parts=[TextPart("done")], usage=RequestUsage(input_tokens=5, output_tokens=1)
        )

    agent = Agent(FunctionModel(model))

    @agent.tool_plain
    def work() -> str:
        events.append("tool")
        return "ok"

    await sessions.update_session(
        session_id, UpdateSession(compaction_threshold_tokens=100, compaction_replay_turns=0)
    )
    session_model(agent.model)
    result = await sessions.start_runner(session_id, agent=agent)
    assert not result.finished and result.output is None
    assert events == ["tool", "summary"]
    rows, summary = await snapshot(database, session_id)
    assert len(rows) == 3 and summary is not None and summary.last_message_seq == 2
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished and result.output == "done"
    assert events == ["tool", "summary", "business"]


async def test_cancellation_closes_temporary_then_main_graph_and_recovers_pending_request(
    database, toolset_lifecycle, heartbeat_observation
):
    session_id = uuid4()
    entered = asyncio.Event()
    heartbeat_tasks, heartbeat_called = heartbeat_observation
    summarizing = True

    async def model(messages, info):
        if COMPACTION_PROMPT in user_texts(messages):
            entered.set()
            await asyncio.Event().wait()
        if summarizing:
            return ModelResponse(parts=[ToolCallPart("work", {}, "call-id")])
        return ModelResponse(parts=[TextPart("recovered")])

    agent = Agent(FunctionModel(model), toolsets=[toolset_lifecycle])

    @agent.tool_plain
    def work() -> str:
        return "ok"

    async def execute():
        async with open_runner(
            session_id, agent=agent, session_factory=database.sessions, heartbeat_interval=0.01
        ) as runner:
            await runner.rebuild_context()
            assert not (await runner.turn(steer=["go"])).finished
            await runner.compact()

    task = asyncio.create_task(execute())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await asyncio.wait_for(heartbeat_called.wait(), 5)
        assert toolset_lifecycle.events == ["enter", "enter"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert toolset_lifecycle.events == ["enter", "enter", "exit", "exit"]
    assert heartbeat_tasks and all(task.done() for task in heartbeat_tasks)
    rows, summary = await snapshot(database, session_id)
    assert len(rows) == 3 and summary is None
    summarizing = False
    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert runner.next_step == "model_request"
        result = await runner.turn()
        assert result.finished and result.output == "recovered"
    assert (await snapshot(database, session_id))[0][:3] == rows

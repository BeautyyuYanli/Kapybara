"""Actual SDK and PostgreSQL output: stream scope, hooks, persistence and recovery."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from pydantic_ai import Agent, ModelRequestNode
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage
from sqlalchemy import event as sql_event

from kapy.agent_runner import (
    InputBatch,
    MessageCommitted,
    OutputEvent,
    TextDelta,
    open_runner,
    summary_context_policy,
)
from kapy.agent_runner.repository import AgentRepository
from kapy.control.sessions import SessionService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
ADAPTER = TypeAdapter(list[OutputEvent])


async def no_inputs():
    return None


async def no_cancel(db):
    return False


async def consume(db):
    return ("go",)


async def history(database, session_id):
    async with database.sessions.begin() as db:
        return await AgentRepository(db).read_history_entries(session_id)


async def test_stream_is_live_hooks_precede_model_and_commits_are_readable(database):
    session_id = uuid4()
    events = []
    order = []

    class ObserveNodes(AbstractCapability):
        async def before_node_run(self, ctx, *, node):
            if isinstance(node, ModelRequestNode):
                order.append("before model")
            return node

    async def model(messages, info):
        order.append("model started")
        yield {0: DeltaThinkingPart(content="think")}
        yield {0: DeltaThinkingPart(signature="signature-only")}
        yield {0: DeltaThinkingPart(content=" carefully")}
        yield "answer"
        # The response is still being generated, but its earlier deltas have
        # already reached the callback and only the accepted request is durable.
        assert any(isinstance(item, TextDelta) for item in events)
        assert len(await history(database, session_id)) == 1
        yield " \n"
        order.append("model ended")

    async def callback(event):
        if isinstance(event, MessageCommitted):
            stored = await history(database, session_id)
            assert event.message == stored[event.message.seq]
        else:
            assert "model ended" not in order
        events.append(deepcopy(event))

    async with open_runner(
        session_id,
        agent=Agent(FunctionModel(stream_function=model), capabilities=[ObserveNodes()]),
        session_factory=database.sessions,
    ) as runner:
        result = await runner.run(
            initial=InputBatch(("go",), consume),
            read_steer=no_inputs,
            consume_cancel=no_cancel,
            on_output=callback,
        )
    assert result.output == "answer \n"
    assert order == ["before model", "model started", "model ended"]
    deltas = [item for item in events if isinstance(item, TextDelta)]
    assert [(item.part_kind, item.op, item.text) for item in deltas] == [
        ("thinking", "replace", "think"),
        ("thinking", "append", " carefully"),
        ("text", "replace", "answer"),
        ("text", "append", " \n"),
    ]
    assert [(item.response_seq, item.part_index) for item in deltas] == [
        (1, 0),
        (1, 0),
        (1, 1),
        (1, 1),
    ]
    assert [item.message.seq for item in events if isinstance(item, MessageCommitted)] == [0, 1]


@pytest.mark.parametrize("first_enabled", [False, True])
async def test_callback_switches_on_retained_native_graph(database, first_enabled):
    session_id = uuid4()
    calls = []
    events = []
    cancel = False

    def response(messages):
        if isinstance(messages[-1].parts[0], ToolReturnPart):
            return ModelResponse(parts=[TextPart("finished")])
        return ModelResponse(parts=[ToolCallPart("work", {}, "work-id")])

    async def ordinary(messages, info):
        calls.append("ordinary")
        return response(messages)

    async def streamed(messages, info):
        calls.append("streamed")
        if isinstance(response(messages).parts[0], ToolCallPart):
            yield {0: DeltaToolCall(name="work", json_args="{}", tool_call_id="work-id")}
        else:
            yield "finished"

    agent = Agent(FunctionModel(ordinary, stream_function=streamed))

    @agent.tool_plain
    def work() -> str:
        nonlocal cancel
        cancel = True
        return "ok"

    async def consume_cancel(db):
        nonlocal cancel
        result, cancel = cancel, False
        return result

    async def callback(event):
        events.append(event)

    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        result = await runner.run(
            initial=InputBatch(("go",), consume),
            read_steer=no_inputs,
            consume_cancel=consume_cancel,
            on_output=callback if first_enabled else None,
        )
        assert not result.finished
        assert runner.next_step == "model_request"
        result = await runner.run(
            read_steer=no_inputs,
            consume_cancel=consume_cancel,
            on_output=None if first_enabled else callback,
        )
        assert result.output == "finished"
        assert calls == (["streamed", "ordinary"] if first_enabled else ["ordinary", "streamed"])
        old_events = list(events)
        await runner.turn(steer=["another turn"])
        assert events == old_events
        assert calls[-1] == "ordinary"
    committed = [item.message.seq for item in events if isinstance(item, MessageCommitted)]
    assert committed == ([0, 1, 2] if first_enabled else [3])


async def test_normalized_checkpoint_dto_matches_read_without_extra_select(database):
    session_id, token = uuid4(), uuid4()
    messages = [
        ModelRequest(
            parts=[
                UserPromptPart(["binary", BinaryContent(data=b"\x00\xff", media_type="image/png")])
            ]
        ),
        ModelResponse(
            parts=[TextPart("answer")],
            finish_reason="stop",
            provider_response_id="provider-id",
            metadata={"nested": {"value": 42}},
            usage=RequestUsage(
                input_tokens=12, output_tokens=4, cache_read_tokens=8, details={"extra": 99}
            ),
        ),
    ]
    statements = []

    def record(connection, cursor, statement, parameters, context, many):
        statements.append(statement)

    async with database.sessions.begin() as db:
        repo = AgentRepository(db)
        await repo.acquire(session_id, token, heartbeat_timeout=60)
        await repo.lock_owned(session_id, token)
        sql_event.listen(database.engine.sync_engine, "before_cursor_execute", record)
        try:
            written = await repo.save_checkpoint(
                session_id, next_step="done", start_seq=0, messages=messages
            )
        finally:
            sql_event.remove(database.engine.sync_engine, "before_cursor_execute", record)
    assert not any(
        "SELECT" in statement.upper() or "RETURNING" in statement.upper()
        for statement in statements
    )
    read = await history(database, session_id)
    assert written == read
    assert ADAPTER.dump_json([MessageCommitted(item) for item in written]) == ADAPTER.dump_json(
        [MessageCommitted(item) for item in read]
    )
    response = read[-1].message
    assert isinstance(response, ModelResponse)
    assert response.usage == RequestUsage(input_tokens=12, output_tokens=4)


@pytest.mark.parametrize("failure", ["consume", "model", "callback"])
async def test_failures_publish_no_uncommitted_response(database, failure):
    session_id = uuid4()
    events = []

    async def accept(db):
        if failure == "consume":
            raise RuntimeError("consume failure")
        return ("go",)

    async def model(messages, info):
        yield "partial"
        if failure == "model":
            raise RuntimeError("model failure")
        yield " rest"

    async def callback(event):
        events.append(event)
        if failure == "callback" and isinstance(event, TextDelta):
            raise RuntimeError("callback failure")

    with pytest.raises(RuntimeError, match=f"{failure} failure"):
        async with open_runner(
            session_id,
            agent=Agent(FunctionModel(stream_function=model)),
            session_factory=database.sessions,
        ) as runner:
            await runner.run(
                initial=InputBatch(("go",), accept),
                read_steer=no_inputs,
                consume_cancel=no_cancel,
                on_output=callback,
            )
    stored = await history(database, session_id)
    assert len(stored) == (0 if failure == "consume" else 1)
    assert [item.message for item in events if isinstance(item, MessageCommitted)] == list(stored)
    if failure != "consume":
        async with open_runner(
            session_id,
            agent=Agent("test"),
            session_factory=database.sessions,
        ) as runner:
            assert (await runner.run(read_steer=no_inputs, consume_cancel=no_cancel)).finished


async def test_compaction_summary_is_excluded_from_business_output(database):
    session_id = uuid4()
    events = []
    summaries = []

    async def summarize(messages, info):
        summaries.append(messages)
        return ModelResponse(parts=[TextPart("private summary")])

    async def business(messages, info):
        yield "business output"

    async def callback(event):
        events.append(event)

    agent = Agent(FunctionModel(summarize, stream_function=business))
    async with open_runner(
        session_id,
        agent=agent,
        context_policy=summary_context_policy(agent, threshold_tokens=1),
        session_factory=database.sessions,
    ) as runner:
        await runner.run(
            initial=InputBatch(("go",), consume),
            read_steer=no_inputs,
            consume_cancel=no_cancel,
            on_output=callback,
        )
    assert len(summaries) == 1
    assert "private summary" not in ADAPTER.dump_json(events).decode()
    assert len(await history(database, session_id)) == 2


async def test_missing_transport_rejected_before_acquiring_lease(database):
    session_id = uuid4()
    sessions = SessionService(database.sessions)
    with pytest.raises(RuntimeError, match="output service"):
        await sessions.start_runner(session_id, agent=Agent("test"), realtime_output=True)
    async with database.sessions.begin() as db:
        from sqlalchemy import text

        assert (await db.execute(text("SELECT count(*) FROM agent_states"))).scalar_one() == 0
    with pytest.raises(RuntimeError, match="output service"):
        await anext(sessions.live(session_id))

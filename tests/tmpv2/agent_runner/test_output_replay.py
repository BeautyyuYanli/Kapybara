"""SessionService integrates real history snapshots and live Valkey broadcasts."""

import asyncio
from contextlib import aclosing
from uuid import uuid4

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kapy.tmpv2.agent_output import AgentOutputService
from kapy.tmpv2.agent_runner import HistoryMessage, MessageCommitted, TextDelta
from kapy.tmpv2.agent_runner.repository import AgentRepository
from kapy.tmpv2.control.sessions import SessionService

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def next_committed(events):
    event = await anext(events)
    assert isinstance(event, MessageCommitted)
    return event.message


def response(text="answer"):
    return ModelResponse(parts=[TextPart(text)])


async def append(database, session_id, messages, start_seq):
    token = uuid4()
    async with database.sessions.begin() as db:
        repo = AgentRepository(db)
        await repo.acquire(session_id, token, heartbeat_timeout=60)
        await repo.lock_owned(session_id, token)
        entries = await repo.save_checkpoint(
            session_id,
            next_step="done",
            start_seq=start_seq,
            messages=messages,
        )
        await repo.release(session_id, token)
    return entries


async def test_session_start_runner_opens_output_across_queued_runs(database, valkey_client):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)

    async def streamed(messages, info):
        yield "one"
        yield " two"

    await sessions.enqueue_input(session_id, "steer", "first")
    await sessions.enqueue_input(session_id, "queued", "second")
    async with outputs.subscribe(session_id) as events:
        result = await sessions.start_runner(
            session_id,
            agent=Agent(FunctionModel(stream_function=streamed)),
            realtime_output=True,
        )
        assert result.output == "one two"
        async with asyncio.timeout(2):
            seen = [await anext(events) for _ in range(8)]
    assert [item.message.seq for item in seen if isinstance(item, MessageCommitted)] == [0, 1, 2, 3]
    assert [item.response_seq for item in seen if isinstance(item, TextDelta)] == [1, 1, 3, 3]


async def test_subscribe_before_history_deduplicates_overlap(database, valkey_client, monkeypatch):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)
    initial = await append(database, session_id, [ModelRequest(parts=[UserPromptPart("go")])], 0)
    original = AgentRepository.read_history_entries
    calls = []

    async def overlap(repo, requested_session, *, after_seq=-1):
        calls.append(after_seq)
        if len(calls) == 1:
            # stream_output must already hold a confirmed subscription, so a
            # concurrent commit belongs to both this snapshot and the channel.
            channel = f"kapy:agent-output:{session_id}"
            assert (await valkey_client.pubsub_numsub(channel))[0][1] == 1
            committed = await append(database, session_id, [response()], 1)
            async with outputs.publisher(session_id, flush_interval=0) as callback:
                await callback(TextDelta(session_id, 1, 0, "text", "replace", "partial"))
                await callback(MessageCommitted(committed[0]))
        return await original(repo, requested_session, after_seq=after_seq)

    monkeypatch.setattr(AgentRepository, "read_history_entries", overlap)
    async with aclosing(sessions.stream_output(session_id)) as events:
        async with asyncio.timeout(2):
            assert (await next_committed(events)) == initial[0]
            assert (await next_committed(events)).seq == 1
            # The old delta/commit still queued in PubSub must be filtered; this
            # new delta is the next visible event, with no extra history query.
            live = TextDelta(session_id, 2, 0, "text", "append", "new")
            async with outputs.publisher(session_id, flush_interval=0) as callback:
                await callback(live)
            assert await anext(events) == live
    assert calls == [-1]


@pytest.mark.parametrize("trigger", ["commit", "delta", "covered_delta"])
async def test_missing_notifications_backfill_history_once(
    database, valkey_client, monkeypatch, trigger
):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)
    await append(database, session_id, [ModelRequest(parts=[UserPromptPart("go")])], 0)
    original = AgentRepository.read_history_entries
    reads = []

    async def observe(repo, requested_session, *, after_seq=-1):
        reads.append(after_seq)
        return await original(repo, requested_session, after_seq=after_seq)

    monkeypatch.setattr(AgentRepository, "read_history_entries", observe)
    async with aclosing(sessions.stream_output(session_id)) as events:
        assert (await next_committed(events)).seq == 0
        missing = [response("first"), ModelRequest(parts=[UserPromptPart("next")])]
        if trigger == "covered_delta":
            missing.append(response("second"))
        entries = await append(database, session_id, missing, 1)
        live = TextDelta(session_id, 3, 0, "text", "append", "new")
        async with outputs.publisher(session_id, flush_interval=0) as callback:
            await callback(MessageCommitted(entries[-1]) if trigger == "commit" else live)
            if trigger != "delta":
                # A sentinel proves that an overlapping commit or covered delta
                # did not survive the backfill. It is not used as a DB cursor.
                await callback(
                    TextDelta(session_id, len(missing) + 1, 0, "text", "append", "sentinel")
                )
        async with asyncio.timeout(2):
            assert [(await next_committed(events)).seq for _ in missing] == list(
                range(1, 1 + len(missing))
            )
            following = await anext(events)
            assert isinstance(following, TextDelta)
            assert following.text == ("new" if trigger == "delta" else "sentinel")
    assert reads == [-1, 0]


async def test_start_cursor_and_reconnect_recover_unpublished_tail(database, valkey_client):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)
    stored = await append(
        database, session_id, [ModelRequest(parts=[UserPromptPart("go")]), response()], 0
    )
    async with aclosing(sessions.stream_output(session_id, start_seq=1)) as events:
        assert (await next_committed(events)) == stored[1]
    tail = await append(
        database, session_id, [ModelRequest(parts=[UserPromptPart("later")]), response("tail")], 2
    )
    async with aclosing(sessions.stream_output(session_id, start_seq=2)) as events:
        assert [(await next_committed(events)) for _ in tail] == list(tail)
    assert (await valkey_client.pubsub_numsub(f"kapy:agent-output:{session_id}"))[0][1] == 0


async def test_replay_releases_only_database_connection_while_yielding_and_listening(
    database, valkey_client
):
    session_id = uuid4()
    await append(database, session_id, [response()], 0)
    engine = create_async_engine(
        database.engine.url,
        connect_args={"options": f"-csearch_path={database.schema},pg_catalog"},
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.2,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(session_factory, output_service=outputs)

    async def independent_transaction():
        async with asyncio.timeout(1), session_factory.begin() as db:
            assert (await db.execute(text("SELECT 1"))).scalar_one() == 1

    try:
        async with aclosing(sessions.stream_output(session_id)) as events:
            assert (await next_committed(events)).seq == 0
            await independent_transaction()  # Generator is paused at its history yield.
            waiting = asyncio.ensure_future(anext(events))
            try:
                await asyncio.sleep(0)  # Resume past replay and into the pending PubSub read.
                assert not waiting.done()
                await independent_transaction()
                live = TextDelta(session_id, 1, 0, "text", "append", "live")
                async with outputs.publisher(session_id, flush_interval=0) as callback:
                    await callback(live)
                async with asyncio.timeout(1):
                    assert await waiting == live
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
    finally:
        await engine.dispose()


async def test_unstarted_session_can_follow_new_execution(database, valkey_client, monkeypatch):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)
    replayed = asyncio.Event()
    original = AgentRepository.read_history_entries

    async def observe(repo, requested_session, *, after_seq=-1):
        result = await original(repo, requested_session, after_seq=after_seq)
        replayed.set()
        return result

    monkeypatch.setattr(AgentRepository, "read_history_entries", observe)

    async def produce():
        await replayed.wait()
        await sessions.enqueue_input(session_id, "steer", "go")
        await sessions.start_runner(session_id, agent=Agent("test"), realtime_output=True)

    task = asyncio.create_task(produce())
    try:
        async with asyncio.timeout(2):
            async with aclosing(sessions.stream_output(session_id)) as events:
                first = await anext(events)
                assert isinstance(first, MessageCommitted) and first.message.seq == 0
                while True:
                    event = await anext(events)
                    if isinstance(event, MessageCommitted) and event.message.seq == 1:
                        break
        await task
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_unfilled_predecessor_gap_fails_and_closes_subscription(database, valkey_client):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)
    await append(database, session_id, [response()], 0)
    async with aclosing(sessions.stream_output(session_id)) as events:
        assert (await next_committed(events)).seq == 0
        async with outputs.publisher(session_id, flush_interval=0) as callback:
            await callback(MessageCommitted(HistoryMessage(session_id, 2, response())))
        async with asyncio.timeout(2):
            with pytest.raises(RuntimeError, match="predecessor"):
                await anext(events)
    assert (await valkey_client.pubsub_numsub(f"kapy:agent-output:{session_id}"))[0][1] == 0


@pytest.mark.parametrize("start_seq", [-1, True, 1.5])
async def test_invalid_history_cursor_rejected_at_iteration(database, valkey_client, start_seq):
    sessions = SessionService(database.sessions, output_service=AgentOutputService(valkey_client))
    with pytest.raises(ValueError):
        await anext(sessions.stream_output(uuid4(), start_seq=start_seq))

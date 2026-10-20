"""SessionService integrates real history snapshots and live Valkey broadcasts."""

import asyncio
import os
from contextlib import aclosing, asynccontextmanager
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from valkey.asyncio import Valkey
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from kapy.agent_history import HistoryMessage
from kapy.agent_history.repository import HistoryRepository
from kapy.agent_output import AgentOutputService, MessageCommitted, OutputEvent, TextDelta
from kapy.control.sessions import SessionService
from kapy.control.sessions.models import SessionRow
from kapy.runner import Recorder, RunnerDeps

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@asynccontextmanager
async def direct_publisher(client, session_id):
    async def publish(event):
        await client.publish(
            f"kapy:agent-output:{session_id}", TypeAdapter(list[OutputEvent]).dump_json([event])
        )

    yield publish


@asynccontextmanager
async def individual_events(sessions, session_id, **kwargs):
    """Assert stream semantics independently of scheduling-dependent batch boundaries."""
    async with aclosing(sessions.live(session_id, **kwargs)) as batches:

        async def flatten():
            async for batch in batches:
                assert batch
                for event in batch:
                    yield event

        async with aclosing(flatten()) as events:
            yield events


async def next_committed(events):
    event = await anext(events)
    assert isinstance(event, MessageCommitted)
    return event.message


def response(text="answer"):
    return ModelResponse(parts=[TextPart(text)])


async def append(database, session_id, messages, start_seq):
    entries = tuple(
        HistoryMessage(session_id, start_seq + index, message)
        for index, message in enumerate(messages)
    )
    async with database.sessions.begin() as db:
        if await db.get(SessionRow, session_id) is None:
            db.add(SessionRow(id=session_id, provider_id=uuid4(), model_name="test"))
            await db.flush()
        await HistoryRepository(db).append_history(session_id, entries)
    return entries


async def test_live_replays_completed_activity_batches(database, valkey_client, seed_session):
    session_id = uuid4()
    await seed_session(session_id, running=True)
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)
    for seq, prompt in ((0, "first"), (2, "second")):
        await Recorder().persist_messages(
            *(
                RunnerDeps(
                    session_id=session_id,
                    session_factory=database.sessions,
                    output_service=outputs,
                    activity_id=session_id,
                    activity_run_id="test-run",
                ),
                [
                    HistoryMessage(session_id, seq, ModelRequest([UserPromptPart(prompt)])),
                    HistoryMessage(session_id, seq + 1, response("one two")),
                ],
            )
        )
    async with individual_events(sessions, session_id) as events:
        async with asyncio.timeout(2):
            assert [(await next_committed(events)).seq for _ in range(4)] == [0, 1, 2, 3]


async def test_subscribe_before_history_deduplicates_overlap(
    database, valkey_client, monkeypatch, seed_session
):
    session_id = uuid4()
    await seed_session(session_id, running=True)
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)
    initial = await append(database, session_id, [ModelRequest(parts=[UserPromptPart("go")])], 0)
    original = HistoryRepository.read_history_entries
    calls = []

    async def overlap(repo, requested_session, *, after_seq=-1):
        calls.append(after_seq)
        if len(calls) == 1:
            # live must already hold a confirmed subscription, so a
            # concurrent commit belongs to both this snapshot and the channel.
            channel = f"kapy:agent-output:{session_id}"
            assert (await valkey_client.pubsub_numsub(channel))[0][1] == 1
            committed = await append(database, session_id, [response()], 1)
            async with direct_publisher(valkey_client, session_id) as callback:
                await callback(TextDelta(session_id, 1, 0, "text", "replace", "partial"))
                await callback(MessageCommitted(committed[0]))
        return await original(repo, requested_session, after_seq=after_seq)

    monkeypatch.setattr(HistoryRepository, "read_history_entries", overlap)
    async with individual_events(sessions, session_id) as events:
        async with asyncio.timeout(2):
            assert (await next_committed(events)) == initial[0]
            assert (await next_committed(events)).seq == 1
            # The old delta/commit still queued in PubSub must be filtered; this
            # new delta is the next visible event, with no extra history query.
            live = TextDelta(session_id, 2, 0, "text", "append", "new")
            async with direct_publisher(valkey_client, session_id) as callback:
                await callback(live)
            assert await anext(events) == live
    assert calls == [-1]


@pytest.mark.parametrize("broadcast_prefix", [False, True])
async def test_missing_notifications_backfill_history_once(
    database, valkey_client, monkeypatch, seed_session, broadcast_prefix
):
    session_id = uuid4()
    await seed_session(session_id, running=True)
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)
    await append(database, session_id, [ModelRequest(parts=[UserPromptPart("go")])], 0)
    original = HistoryRepository.read_history_entries
    reads = []

    async def observe(repo, requested_session, *, after_seq=-1):
        reads.append(after_seq)
        return await original(repo, requested_session, after_seq=after_seq)

    monkeypatch.setattr(HistoryRepository, "read_history_entries", observe)
    async with individual_events(sessions, session_id) as events:
        assert (await next_committed(events)).seq == 0
        missing = [
            response("first"),
            ModelRequest(parts=[UserPromptPart("next")]),
            response("second"),
        ]
        entries = await append(database, session_id, missing, 1)
        live = TextDelta(session_id, 4, 0, "text", "append", "new")
        batch = [MessageCommitted(entries[-1])]
        if broadcast_prefix:
            batch.append(MessageCommitted(entries[0]))
        # This preview proves the overlapping commit did not survive backfill.
        await valkey_client.publish(
            f"kapy:agent-output:{session_id}",
            TypeAdapter(list[OutputEvent]).dump_json([*batch, live]),
        )
        async with asyncio.timeout(2):
            assert [(await next_committed(events)).seq for _ in missing] == list(
                range(1, 1 + len(missing))
            )
            assert await anext(events) == live
    assert reads == [-1, 1 if broadcast_prefix else 0]


async def test_evicted_message_notifications_backfill_on_retained_gap(database, valkey_client):
    session_id = uuid4()
    sessions = SessionService(database.sessions, output_service=AgentOutputService(valkey_client))
    initial = (await append(database, session_id, [response("initial")], 0))[0]
    async with aclosing(sessions.live(session_id)) as batches:
        assert await anext(batches) == [MessageCommitted(initial)]
        entries = await append(
            database, session_id, [response(str(seq)) for seq in range(1, 258)], 1
        )
        preview = TextDelta(session_id, 258, 0, "text", "replace", "next")
        # The consumer is paused. The first two notifications are evicted before
        # any handoff, so the retained message must recover the missing prefix.
        expected: list[OutputEvent] = [*[MessageCommitted(entry) for entry in entries], preview]
        await valkey_client.publish(
            f"kapy:agent-output:{session_id}", TypeAdapter(list[OutputEvent]).dump_json(expected)
        )
        async with asyncio.timeout(2):  # Before the default five-second poll.
            assert await anext(batches) == expected


async def test_poll_recovers_evicted_tail(database, valkey_client):
    session_id = uuid4()
    channel = f"kapy:agent-output:{session_id}"
    sessions = SessionService(
        database.sessions, output_service=AgentOutputService(valkey_client), live_poll_interval=0.5
    )
    initial = HistoryMessage(session_id, 0, response("initial"))
    async with database.sessions.begin() as db:
        await HistoryRepository(db).append_history(session_id, [initial])
    async with aclosing(sessions.live(session_id)) as batches:
        assert await anext(batches) == [MessageCommitted(initial)]
        final = HistoryMessage(session_id, 1, response("final"))
        async with database.sessions.begin() as db:
            await HistoryRepository(db).append_history(session_id, [final])
        previews: list[OutputEvent] = [
            TextDelta(session_id, seq, 0, "text", "replace", str(seq)) for seq in range(2, 258)
        ]
        # Only previews survive this batch. They cannot advance the message
        # cursor or trigger backfill, and no later broadcast will announce final.
        await valkey_client.publish(
            channel, TypeAdapter(list[OutputEvent]).dump_json([MessageCommitted(final), *previews])
        )
        async with asyncio.timeout(2):
            received = [await anext(batches), await anext(batches)]
            assert previews in received
            assert [MessageCommitted(final)] in received
        assert (await valkey_client.pubsub_numsub(channel))[0][1] == 1


async def test_previews_and_contiguous_messages_do_not_read_history(
    database, valkey_client, monkeypatch
):
    session_id = uuid4()
    initial = (await append(database, session_id, [response("initial")], 0))[0]
    sessions = SessionService(database.sessions, output_service=AgentOutputService(valkey_client))
    original = HistoryRepository.read_history_entries
    reads = []

    async def observe(repo, requested_session, *, after_seq=-1):
        reads.append(after_seq)
        return await original(repo, requested_session, after_seq=after_seq)

    monkeypatch.setattr(HistoryRepository, "read_history_entries", observe)
    async with aclosing(sessions.live(session_id)) as batches:
        assert await anext(batches) == [MessageCommitted(initial)]
        near = TextDelta(session_id, 1, 0, "text", "replace", "near")
        far = TextDelta(session_id, 8, 0, "text", "append", "far")
        await valkey_client.publish(
            f"kapy:agent-output:{session_id}",
            TypeAdapter(list[OutputEvent]).dump_json([near, far]),
        )
        async with asyncio.timeout(2):
            assert await anext(batches) == [near, far]
        assert reads == [-1]

        final = await append(database, session_id, [response(str(seq)) for seq in (1, 2, 3)], 1)
        covered = TextDelta(session_id, 2, 0, "text", "append", "obsolete")
        following = TextDelta(session_id, 5, 0, "text", "append", "next")
        await valkey_client.publish(
            f"kapy:agent-output:{session_id}",
            TypeAdapter(list[OutputEvent]).dump_json(
                [
                    MessageCommitted(initial),
                    *[MessageCommitted(e) for e in reversed(final)],
                    covered,
                    following,
                ]
            ),
        )
        async with asyncio.timeout(2):
            assert await anext(batches) == [
                *[MessageCommitted(entry) for entry in final],
                following,
            ]
        assert reads == [-1]

    # The client applied only through 1 despite this connection sending through 3.
    async with aclosing(sessions.live(session_id, after_seq=1)) as batches:
        assert await anext(batches) == [MessageCommitted(entry) for entry in final[1:]]
    assert reads == [-1, 1]


async def test_start_cursor_and_reconnect_recover_unpublished_tail(
    database, valkey_client, seed_session
):
    session_id = uuid4()
    await seed_session(session_id, running=True)
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)
    stored = await append(
        database, session_id, [ModelRequest(parts=[UserPromptPart("go")]), response()], 0
    )
    async with individual_events(sessions, session_id, after_seq=0) as events:
        assert (await next_committed(events)) == stored[1]
    tail = await append(
        database, session_id, [ModelRequest(parts=[UserPromptPart("later")]), response("tail")], 2
    )
    async with individual_events(sessions, session_id, after_seq=1) as events:
        assert [(await next_committed(events)) for _ in tail] == list(tail)
    assert (await valkey_client.pubsub_numsub(f"kapy:agent-output:{session_id}"))[0][1] == 0


async def test_replay_releases_only_database_connection_while_yielding_and_listening(
    database, valkey_client, seed_session
):
    session_id = uuid4()
    await seed_session(session_id, running=True)
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
        async with individual_events(sessions, session_id) as events:
            assert (await next_committed(events)).seq == 0
            await independent_transaction()  # Generator is paused at its history yield.
            waiting = asyncio.ensure_future(anext(events))
            try:
                await asyncio.sleep(0)  # Resume past replay and into the pending PubSub read.
                assert not waiting.done()
                await independent_transaction()
                live = TextDelta(session_id, 1, 0, "text", "append", "live")
                async with direct_publisher(valkey_client, session_id) as callback:
                    await callback(live)
                async with asyncio.timeout(1):
                    assert await waiting == live
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
    finally:
        await engine.dispose()


async def test_unstarted_session_can_follow_new_execution(
    database, valkey_client, monkeypatch, seed_session
):
    session_id = uuid4()
    await seed_session(session_id, running=True)
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs, live_poll_interval=0.05)
    replayed = asyncio.Event()
    original = HistoryRepository.read_history_entries

    async def observe(repo, requested_session, *, after_seq=-1):
        result = await original(repo, requested_session, after_seq=after_seq)
        replayed.set()
        return result

    monkeypatch.setattr(HistoryRepository, "read_history_entries", observe)

    async def produce():
        await replayed.wait()
        await Recorder().persist_messages(
            *(
                RunnerDeps(
                    session_id=session_id,
                    session_factory=database.sessions,
                    output_service=outputs,
                    activity_id=session_id,
                    activity_run_id="test-run",
                ),
                [
                    HistoryMessage(session_id, 0, ModelRequest([UserPromptPart("go")])),
                    HistoryMessage(session_id, 1, response()),
                ],
            )
        )

    task = asyncio.create_task(produce())
    try:
        async with asyncio.timeout(2):
            async with individual_events(sessions, session_id) as events:
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


async def test_gaps_are_valid_and_later_broadcast_backfills(database, valkey_client, seed_session):
    session_id = uuid4()
    await seed_session(session_id, running=True)
    sessions = SessionService(database.sessions, output_service=AgentOutputService(valkey_client))
    await append(database, session_id, [response()], 0)
    async with individual_events(sessions, session_id) as events:
        assert (await next_committed(events)).seq == 0
        one = (await append(database, session_id, [response("one")], 2))[0]
        two = (await append(database, session_id, [response("two")], 5))[0]
        async with direct_publisher(valkey_client, session_id) as callback:
            await callback(MessageCommitted(two))
        async with asyncio.timeout(2):
            assert await next_committed(events) == one
            assert await next_committed(events) == two


async def test_poll_recovers_silent_tail_without_cancelling_subscription(database, valkey_client):
    session_id = uuid4()
    sessions = SessionService(
        database.sessions, output_service=AgentOutputService(valkey_client), live_poll_interval=0.02
    )
    await append(database, session_id, [response()], 0)
    async with individual_events(sessions, session_id) as events:
        assert (await next_committed(events)).seq == 0
        waiting = asyncio.create_task(anext(events))
        try:
            await asyncio.sleep(0.07)  # Several empty polls must preserve the pending read.
            entry = (await append(database, session_id, [response("silent")], 1))[0]
            async with asyncio.timeout(2):
                assert await waiting == MessageCommitted(entry)
                async with direct_publisher(valkey_client, session_id) as callback:
                    await callback(MessageCommitted(entry))
                    preview = TextDelta(session_id, 2, 0, "text", "append", "next")
                    await callback(preview)
                assert await anext(events) == preview
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
    assert (await valkey_client.pubsub_numsub(f"kapy:agent-output:{session_id}"))[0][1] == 0


@pytest.mark.parametrize("interval", [0, -1, float("nan"), float("inf")])
async def test_invalid_poll_interval(database, interval):
    with pytest.raises(ValueError, match="finite and positive"):
        SessionService(database.sessions, live_poll_interval=interval)


@pytest.mark.parametrize("after_seq", [-2, True, 1.5])
async def test_invalid_history_cursor_rejected_at_iteration(database, valkey_client, after_seq):
    sessions = SessionService(database.sessions, output_service=AgentOutputService(valkey_client))
    with pytest.raises(ValueError):
        await anext(sessions.live(uuid4(), after_seq=after_seq))


async def test_delta_traffic_does_not_postpone_poll(database, valkey_client):
    session_id = uuid4()
    sessions = SessionService(
        database.sessions, output_service=AgentOutputService(valkey_client), live_poll_interval=0.03
    )
    await append(database, session_id, [response()], 0)

    async def previews():
        async with direct_publisher(valkey_client, session_id) as callback:
            while True:
                await callback(TextDelta(session_id, 1, 0, "text", "append", "preview"))
                await asyncio.sleep(0.001)

    async with individual_events(sessions, session_id) as events:
        assert (await next_committed(events)).seq == 0
        producer = asyncio.create_task(previews())
        try:
            await append(database, session_id, [response("complete")], 1)
            async with asyncio.timeout(1):
                while not isinstance(event := await anext(events), MessageCommitted):
                    assert event.response_seq == 1
                assert event.message.seq == 1
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)


async def test_poll_error_propagates_and_cleans_pending_subscription(
    database, valkey_client, monkeypatch
):
    session_id = uuid4()
    sessions = SessionService(
        database.sessions, output_service=AgentOutputService(valkey_client), live_poll_interval=0.02
    )
    await append(database, session_id, [response()], 0)
    async with individual_events(sessions, session_id) as events:
        assert (await next_committed(events)).seq == 0

        async def failed_read(*args, **kwargs):
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(HistoryRepository, "read_history_entries", failed_read)
        async with asyncio.timeout(1):
            with pytest.raises(RuntimeError, match="database unavailable"):
                await anext(events)
    assert (await valkey_client.pubsub_numsub(f"kapy:agent-output:{session_id}"))[0][1] == 0


async def test_live_subscription_disconnect_propagates_and_cleans_up(database, valkey_client):
    session_id = uuid4()
    await append(database, session_id, [response()], 0)
    async with Valkey.from_url(
        os.environ.get("KAPY_VALKEY_URL", "valkey://127.0.0.1:56379/0"),
        client_name=f"live-test:{session_id}",
    ) as subscriber_client:
        sessions = SessionService(
            database.sessions,
            output_service=AgentOutputService(subscriber_client),
            live_poll_interval=0.02,
        )
        async with individual_events(sessions, session_id) as events:
            assert (await next_committed(events)).seq == 0
            waiting = asyncio.create_task(anext(events))
            try:
                await asyncio.sleep(0.05)
                assert not waiting.done()
                client_id = next(
                    client["id"]
                    for client in await valkey_client.client_list()
                    if client["name"] == f"live-test:{session_id}"
                )
                await valkey_client.client_kill_filter(_id=client_id)
                async with asyncio.timeout(1):
                    with pytest.raises(ValkeyConnectionError):
                        await waiting
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)
        assert await subscriber_client.ping()
    assert (await valkey_client.pubsub_numsub(f"kapy:agent-output:{session_id}"))[0][1] == 0


async def test_live_subscriber_timeout_is_not_poll_timeout(database, valkey_client, monkeypatch):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs, live_poll_interval=0.02)
    subscribe = outputs.subscribe
    failure = TimeoutError("subscription read failed")
    raised = asyncio.Event()

    @asynccontextmanager
    async def failing_subscription(requested_session):
        async with subscribe(requested_session):

            async def events():
                # Let live complete several ordinary empty poll timeouts first.
                await asyncio.sleep(0.07)
                raised.set()
                raise failure
                yield  # Make this an async generator with the real iterator contract.

            async with aclosing(events()) as iterator:
                yield iterator

    monkeypatch.setattr(outputs, "subscribe", failing_subscription)
    async with individual_events(sessions, session_id) as events:
        async with asyncio.timeout(1):
            with pytest.raises(TimeoutError) as caught:
                await anext(events)
        assert raised.is_set()
        assert caught.value is failure
    assert (await valkey_client.pubsub_numsub(f"kapy:agent-output:{session_id}"))[0][1] == 0


async def test_live_hands_off_whole_history_and_backfilled_batch(database, valkey_client):
    session_id = uuid4()
    initial = await append(database, session_id, [response("zero"), response("one")], 0)
    sessions = SessionService(database.sessions, output_service=AgentOutputService(valkey_client))
    async with aclosing(sessions.live(session_id)) as batches:
        assert await anext(batches) == [MessageCommitted(entry) for entry in initial]
        tail = await append(database, session_id, [response("two"), response("three")], 2)
        # Sub retains seq 2's preview because only seq 3 was broadcast. The gap
        # backfill must remove that preview from live's still-undelivered result.
        preview = TextDelta(session_id, 2, 0, "text", "replace", "temporary")
        future = TextDelta(session_id, 4, 0, "text", "append", "next")
        await valkey_client.publish(
            f"kapy:agent-output:{session_id}",
            TypeAdapter(list[OutputEvent]).dump_json([preview, MessageCommitted(tail[-1]), future]),
        )
        async with asyncio.timeout(2):
            assert await anext(batches) == [*[MessageCommitted(entry) for entry in tail], future]


async def test_backfill_orders_messages_and_filters_covered_deltas(database, valkey_client):
    session_id = uuid4()
    initial = (await append(database, session_id, [response("initial")], 0))[0]
    sessions = SessionService(database.sessions, output_service=AgentOutputService(valkey_client))
    async with aclosing(sessions.live(session_id)) as batches:
        assert await anext(batches) == [MessageCommitted(initial)]
        final = (await append(database, session_id, [response("final")], 2))[0]
        complete = MessageCommitted(HistoryMessage(session_id, 3, response("snapshot")))
        async with database.sessions.begin() as db:
            await HistoryRepository(db).append_history(session_id, [complete.message])
        other = TextDelta(session_id, 4, 0, "text", "replace", "other")
        following = TextDelta(session_id, 3, 0, "text", "append", "following")
        await valkey_client.publish(
            f"kapy:agent-output:{session_id}",
            TypeAdapter(list[OutputEvent]).dump_json(
                [other, complete, MessageCommitted(final), following]
            ),
        )
        async with asyncio.timeout(2):
            assert await anext(batches) == [MessageCommitted(final), complete, other]


@pytest.mark.parametrize("seq", [1, 3])
async def test_live_keeps_broadcast_usage_details(database, valkey_client, seq):
    from pydantic_ai.usage import RequestUsage

    session_id = uuid4()
    sessions = SessionService(database.sessions, output_service=AgentOutputService(valkey_client))
    initial = (await append(database, session_id, [response("initial")], 0))[0]
    async with individual_events(sessions, session_id) as events:
        assert await next_committed(events) == initial
        full = ModelResponse(
            [TextPart("full")],
            usage=RequestUsage(input_tokens=100, output_tokens=20, details={"extra": 7}),
        )
        entry = (await append(database, session_id, [full], seq))[0]
        async with direct_publisher(valkey_client, session_id) as publish:
            await publish(MessageCommitted(entry))
        async with asyncio.timeout(2):
            assert await next_committed(events) == entry


@pytest.mark.parametrize("broadcast", [False, True])
async def test_gap_recovers_complete_message_and_filters_late_preview(
    database, valkey_client, broadcast
):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs, live_poll_interval=0.02)
    complete = HistoryMessage(session_id, 1, response("first"))
    await append(database, session_id, [complete.message], complete.seq)
    async with individual_events(sessions, session_id) as events:
        assert await next_committed(events) == complete
        final = (await append(database, session_id, [response("final")], 3))[0]
        async with database.sessions.begin() as db:
            assert await HistoryRepository(db).read_history_entries(session_id) == (
                complete,
                final,
            )
        if broadcast:
            async with direct_publisher(valkey_client, session_id) as publish:
                await publish(MessageCommitted(final))
        async with asyncio.timeout(2):
            assert await next_committed(events) == final
            future = TextDelta(session_id, 4, 0, "text", "replace", "future")
            async with direct_publisher(valkey_client, session_id) as publish:
                await publish(MessageCommitted(complete))
                await publish(TextDelta(session_id, 1, 0, "text", "append", "late"))
                await publish(future)
            assert await anext(events) == future

"""Real Temporal codecs, PostgreSQL write boundaries and committed full-message broadcasts."""

import asyncio
from contextlib import aclosing, asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic_ai.durable_exec.temporal import PydanticAIPayloadConverter
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage
from sqlalchemy import event, text
from sqlmodel import col, select
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError

from kapy.agent_runner import HistoryMessage, MessageCommitted
from kapy.agent_runner.models import AgentHistoryRow, AgentStateRow
from kapy.agent_runner.repository import AgentRepository
from kapy.control.sessions import SessionService
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository, lock_session
from kapy.runner_duarable.activities import RunnerActivities
from kapy.runner_duarable.types import MessageBatch

pytestmark = pytest.mark.asyncio


async def test_message_payload_round_trip_preserves_sdk_binary_and_usage():
    session_id = uuid4()
    message = ModelRequest(
        parts=[
            UserPromptPart(["image", BinaryContent(data=b"\xff\x00\xfe", media_type="image/png")])
        ],
        metadata={"seq": 4, "authoritative": True, "source": "upload"},
    )
    response = ModelResponse(
        parts=[TextPart("done")],
        usage=RequestUsage(input_tokens=10, output_tokens=2, output_reasoning_tokens=0),
    )
    data = MessageBatch(
        session_id=session_id,
        messages=[
            HistoryMessage(session_id, 4, True, message),
            HistoryMessage(session_id, 5, True, response),
        ],
    )
    converter = DataConverter(payload_converter_class=PydanticAIPayloadConverter)
    restored = (await converter.decode(await converter.encode([data]), [MessageBatch]))[0]
    expected = ModelMessagesTypeAdapter.validate_json(
        ModelMessagesTypeAdapter.dump_json([message, response])
    )
    assert restored.messages == [
        HistoryMessage(session_id, 4, True, expected[0]),
        HistoryMessage(session_id, 5, True, expected[1]),
    ]
    assert restored.messages[0].message.parts[0].content[1].data == b"\xff\x00\xfe"
    assert restored.messages[1].message.usage.__dict__["output_reasoning_tokens"] == 0


@pytest.mark.integration
async def test_provisional_overwrite_and_broadcast_complete_input_after_commit(
    runner_database, monkeypatch
):
    session_id, outputs = runner_database.session_id, runner_database.outputs
    activities = RunnerActivities(runner_database.sessions, outputs)
    response = ModelResponse(
        parts=[TextPart("old")],
        # Payload fields deliberately differ: columns must never be derived from JSON.
        metadata={"seq": "opaque", "authoritative": "opaque", "old_key": "old"},
        finish_reason="stop",
        provider_name="provider",
        provider_details={"key": "value"},
        usage=RequestUsage(
            input_tokens=100, output_tokens=20, cache_read_tokens=50, details={"extra": 7}
        ),
    )
    entry = HistoryMessage(session_id, 10, False, response)
    first = MessageBatch(session_id=session_id, messages=[entry])
    publish = outputs._publish

    async def check_committed(requested_session, payload):
        async with runner_database.sessions.begin() as db:
            row = await db.get(AgentHistoryRow, (session_id, 10))
            assert row is not None
        await publish(requested_session, payload)

    monkeypatch.setattr(outputs, "_publish", check_committed)
    async with outputs.subscribe(session_id) as events:
        await activities.record_messages(first)
        assert await anext(events) == [MessageCommitted(entry)]
        await activities.record_messages(first)
        assert await anext(events) == [MessageCommitted(entry)]
        async with runner_database.sessions.begin() as db:
            row = await db.get(AgentHistoryRow, (session_id, 10))
            assert row is not None and not row.authoritative
            created_at = row.created_at
            assert (row.finish_reason, row.input_tokens, row.output_tokens) == ("stop", 100, 20)
        replacement = ModelRequest(
            parts=[UserPromptPart(["new", BinaryContent(data=b"\xff", media_type="image/png")])],
            metadata={"unrelated": "content"},
            instructions="updated instructions",
        )
        replacement = ModelMessagesTypeAdapter.validate_json(
            ModelMessagesTypeAdapter.dump_json([replacement])
        )[0]
        updated_entry = HistoryMessage(session_id, 10, False, replacement)
        await activities.record_messages(
            MessageBatch(session_id=session_id, messages=[updated_entry])
        )
        assert await anext(events) == [MessageCommitted(updated_entry)]
    async with runner_database.sessions.begin() as db:
        row = await db.get(AgentHistoryRow, (session_id, 10))
        assert row is not None and row.created_at == created_at and not row.authoritative
        assert row.kind == "request"
        assert (row.finish_reason, row.input_tokens, row.output_tokens) == (None, None, None)
        assert await AgentRepository(db).read_history_entries(session_id) == (updated_entry,)
        assert await AgentRepository(db).read_history_entries(session_id, after_seq=10) == ()
        assert await db.get(AgentStateRow, session_id) is None
        assert await SessionRepository(db).read_runner_state(session_id) == (None, 0)

    # Clearing usage must also work when overwriting one response with another.
    replacement_response = deepcopy(response)
    replacement_response.usage = RequestUsage()
    replacement_response.finish_reason = None
    await activities.record_messages(first)
    await activities.record_messages(
        MessageBatch(session_id=session_id, messages=[replace(entry, message=replacement_response)])
    )
    async with runner_database.sessions.begin() as db:
        row = await db.get(AgentHistoryRow, (session_id, 10))
        assert row is not None and row.kind == "response" and row.created_at == created_at
        assert (row.finish_reason, row.input_tokens, row.output_tokens) == (None, None, None)


@pytest.mark.integration
@pytest.mark.parametrize(
    "invalid",
    [
        {"seq": None},
        {"seq": True},
        {"seq": -1},
        {"seq": "1"},
        {"seq": 1.5},
        {"seq": 0},
        {"authoritative": 1},
        {"session_id": uuid4()},
    ],
)
async def test_invalid_batch_does_not_write_or_broadcast(runner_database, monkeypatch, invalid):
    outputs = runner_database.outputs
    published = []

    async def publish(*args):
        published.append(args)

    monkeypatch.setattr(outputs, "_publish", publish)
    session_id = runner_database.session_id
    valid = HistoryMessage(session_id, 0, False, ModelRequest(parts=[UserPromptPart("valid")]))
    other = replace(valid, seq=1, **{k: v for k, v in invalid.items() if k != "seq"})
    if "seq" in invalid:
        other = replace(other, seq=invalid["seq"])
    data = MessageBatch.model_construct(session_id=session_id, messages=[valid, other])
    with pytest.raises(ApplicationError) as error:
        await RunnerActivities(runner_database.sessions, outputs).record_messages(data)
    assert error.value.type == "InvalidHistory" and error.value.non_retryable
    assert published == []
    async with runner_database.sessions.begin() as db:
        assert await AgentRepository(db).read_history_entries(session_id) == ()


def history(session_id, seq, authoritative, content="message"):
    return HistoryMessage(session_id, seq, authoritative, ModelRequest([UserPromptPart(content)]))


@pytest.mark.integration
async def test_boundary_filters_in_input_order_and_seals_old_gaps(runner_database, monkeypatch):
    session_id = runner_database.session_id
    activities = RunnerActivities(runner_database.sessions, runner_database.outputs)
    initial = [history(session_id, 0, True), history(session_id, 10, True)]
    provisional = [history(session_id, seq, False) for seq in (11, 12, 14, 18)]
    await activities.record_messages(
        MessageBatch(session_id=session_id, messages=initial + provisional)
    )
    async with runner_database.sessions.begin() as db:
        before = await db.get(AgentHistoryRow, (session_id, 12))
        created_at = before.created_at

    published = []

    @asynccontextmanager
    async def publisher(requested_session):
        assert requested_session == session_id
        # The lock and transaction must already be released before publishing.
        async with runner_database.sessions.begin() as db:
            await db.execute(
                select(SessionRow.id)
                .where(col(SessionRow.id) == session_id)
                .with_for_update(nowait=True)
            )
            stored = await AgentRepository(db).read_history_entries(session_id)
            assert [(e.seq, e.authoritative) for e in stored] == [
                (0, True),
                (10, True),
                (12, True),
                (15, True),
                (16, False),
                (18, False),
            ]

        async def publish(message):
            published.append(message)

        yield publish

    monkeypatch.setattr(runner_database.outputs, "publisher", publisher)
    batch = [
        history(session_id, 5, True, "old gap"),
        history(session_id, 10, True, "changed old A"),
        history(session_id, 15, True),
        history(session_id, 12, True),
        history(session_id, 13, False),
        history(session_id, 16, False),
    ]
    await activities.record_messages(MessageBatch(session_id=session_id, messages=batch))
    assert published == [MessageCommitted(batch[i]) for i in (2, 3, 5)]
    async with runner_database.sessions.begin() as db:
        stored = await AgentRepository(db).read_history_entries(session_id)
        assert stored[:2] == tuple(initial)
        assert (await db.get(AgentHistoryRow, (session_id, 12))).created_at == created_at
        assert stored[-1] == provisional[-1]

    def unexpected_publisher(*args):
        pytest.fail("discarded inputs must not create a publisher")

    monkeypatch.setattr(runner_database.outputs, "publisher", unexpected_publisher)
    await activities.record_messages(
        MessageBatch(
            session_id=session_id,
            messages=[
                history(session_id, 10, False, "downgrade"),
                history(session_id, 12, True, "different retry"),
                history(session_id, 13, True, "newly sealed gap"),
                batch[2],
            ],
        )
    )
    await activities.record_messages(MessageBatch(session_id=session_id, messages=[]))
    async with runner_database.sessions.begin() as db:
        assert await AgentRepository(db).read_history_entries(session_id) == stored


@pytest.mark.integration
async def test_upsert_borrows_transaction_and_returns_accepted_inputs(runner_database):
    session_id = runner_database.session_id
    initial = [
        history(session_id, 0, True),
        history(session_id, 1, False),
        history(session_id, 4, False),
    ]
    async with runner_database.sessions.begin() as db:
        await lock_session(db, session_id)
        assert await AgentRepository(db).upsert_history(session_id, initial) == tuple(initial)
    with pytest.raises(RuntimeError, match="rollback"):
        async with runner_database.sessions.begin() as db:
            await lock_session(db, session_id)
            entry = history(session_id, 3, True)
            assert await AgentRepository(db).upsert_history(session_id, [entry]) == (entry,)
            assert [e.seq for e in await AgentRepository(db).read_history_entries(session_id)] == [
                0,
                3,
                4,
            ]
            raise RuntimeError("rollback")
    async with runner_database.sessions.begin() as db:
        assert await AgentRepository(db).read_history_entries(session_id) == tuple(initial)


@pytest.mark.integration
async def test_discarded_nonempty_batch_cleans_existing_invalid_prefix(runner_database):
    session_id = runner_database.session_id
    async with runner_database.sessions.begin() as db:
        await lock_session(db, session_id)
        await AgentRepository(db).upsert_history(
            session_id, [history(session_id, 0, False), history(session_id, 2, False)]
        )
        # Construct legacy/corrupt storage explicitly; production writes cannot create N,A.
        await db.execute(text("UPDATE agent_history SET authoritative = true WHERE seq = 2"))
        assert await AgentRepository(db).upsert_history(session_id, []) == ()
        assert [e.seq for e in await AgentRepository(db).read_history_entries(session_id)] == [0, 2]
        assert (
            await AgentRepository(db).upsert_history(session_id, [history(session_id, 1, True)])
            == ()
        )
        assert [e.seq for e in await AgentRepository(db).read_history_entries(session_id)] == [2]


@pytest.mark.integration
async def test_record_missing_session_is_non_retryable_and_does_not_publish(
    runner_database, monkeypatch
):
    session_id = uuid4()

    def unexpected_publisher(*args):
        pytest.fail("missing sessions must not publish")

    monkeypatch.setattr(runner_database.outputs, "publisher", unexpected_publisher)
    with pytest.raises(ApplicationError) as error:
        await RunnerActivities(runner_database.sessions, runner_database.outputs).record_messages(
            MessageBatch(session_id=session_id, messages=[history(session_id, 0, True)])
        )
    assert error.value.type == "SessionNotFound" and error.value.non_retryable
    async with runner_database.sessions.begin() as db:
        assert await AgentRepository(db).read_history_entries(session_id) == ()


@pytest.mark.integration
async def test_record_serializes_session_writes_before_reading_boundary(runner_database):
    session_id, other_id = runner_database.session_id, uuid4()
    activities = RunnerActivities(runner_database.sessions, runner_database.outputs)
    async with runner_database.sessions.begin() as db:
        db.add(SessionRow(id=other_id, provider_id=uuid4(), model_name="test"))
    attempted_lock = asyncio.Event()
    engine = runner_database.sessions.kw["bind"]

    def before_execute(connection, cursor, statement, parameters, context, executemany):
        if "FOR UPDATE" in statement:
            attempted_lock.set()

    task = None
    try:
        async with runner_database.sessions.begin() as db:
            await lock_session(db, session_id)
            await AgentRepository(db).upsert_history(session_id, [history(session_id, 2, True)])
            event.listen(engine.sync_engine, "before_cursor_execute", before_execute)
            task = asyncio.create_task(
                activities.record_messages(
                    MessageBatch(session_id=session_id, messages=[history(session_id, 1, False)])
                )
            )
            async with asyncio.timeout(2):
                await attempted_lock.wait()
                # A different session must commit while the first writer holds its lock.
                await activities.record_messages(
                    MessageBatch(session_id=other_id, messages=[history(other_id, 1, False)])
                )
            assert not task.done()
        async with asyncio.timeout(2):
            await task
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_execute)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with runner_database.sessions.begin() as db:
        assert [
            (e.seq, e.authoritative)
            for e in await AgentRepository(db).read_history_entries(session_id)
        ] == [(2, True)]
        assert [
            (e.seq, e.authoritative)
            for e in await AgentRepository(db).read_history_entries(other_id)
        ] == [(1, False)]


@pytest.mark.integration
async def test_commit_before_broadcast_failure_recovers_without_retry_publication(
    runner_database, monkeypatch
):
    session_id, outputs = runner_database.session_id, runner_database.outputs
    activities = RunnerActivities(runner_database.sessions, outputs)
    sessions = SessionService(
        runner_database.sessions, output_service=outputs, live_poll_interval=0.02
    )
    provisional = history(session_id, 1, False)
    await activities.record_messages(MessageBatch(session_id=session_id, messages=[provisional]))
    final = history(session_id, 3, True)
    data = MessageBatch(session_id=session_id, messages=[final])

    def lose_broadcast(*args):
        raise RuntimeError("lost before broadcast")

    async with aclosing(sessions.live(session_id)) as batches:
        assert await anext(batches) == [MessageCommitted(provisional)]
        monkeypatch.setattr(outputs, "publisher", lose_broadcast)
        with pytest.raises(RuntimeError, match="lost before broadcast"):
            await activities.record_messages(data)
        # Retry must not even open the still-failing publisher for the committed A.
        await activities.record_messages(data)
        async with asyncio.timeout(2):
            assert await anext(batches) == [MessageCommitted(final)]

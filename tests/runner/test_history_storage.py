"""PostgreSQL write boundaries and committed full-message broadcasts."""

import asyncio
from contextlib import aclosing, asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.usage import RequestUsage
from sqlalchemy import event
from sqlmodel import col, select

from kapy.agent_history import HistoryMessage
from kapy.agent_history.models import AgentHistoryRow
from kapy.agent_history.repository import HistoryRepository
from kapy.agent_output import MessageCommitted
from kapy.control.sessions import SessionService
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository, lock_session
from kapy.runner import Recorder, RunnerDeps

pytestmark = pytest.mark.asyncio


@pytest.mark.integration
async def test_stored_message_is_immutable_and_broadcast_preserves_complete_input(
    runner_database, monkeypatch
):
    session_id, outputs = runner_database.session_id, runner_database.outputs
    response = ModelResponse(
        parts=[TextPart("original")],
        metadata={"seq": "opaque", "source": "business"},
        finish_reason="stop",
        provider_name="provider",
        provider_details={"key": "value"},
        usage=RequestUsage(
            input_tokens=100, output_tokens=20, cache_read_tokens=50, details={"extra": 7}
        ),
    )
    entry = HistoryMessage(session_id, 10, response)
    published = []
    publish = outputs._publish

    async def check_committed(requested_session, payload):
        async with runner_database.sessions.begin() as db:
            row = await db.get(AgentHistoryRow, (session_id, 10))
            assert row is not None
        published.append(payload)
        await publish(requested_session, payload)

    monkeypatch.setattr(outputs, "_publish", check_committed)
    async with outputs.subscribe(session_id) as events:
        batch = (
            RunnerDeps(
                session_id=session_id,
                session_factory=runner_database.sessions,
                output_service=runner_database.outputs,
            ),
            [entry],
        )
        await Recorder().persist_messages(*batch)
        assert await anext(events) == [MessageCommitted(entry)]
        async with runner_database.sessions.begin() as db:
            row = await db.get(AgentHistoryRow, (session_id, 10))
            created_at = row.created_at
            assert (row.finish_reason, row.input_tokens, row.output_tokens) == ("stop", 100, 20)
        await Recorder().persist_messages(*batch)
        await Recorder().persist_messages(
            *(
                RunnerDeps(
                    session_id=session_id,
                    session_factory=runner_database.sessions,
                    output_service=runner_database.outputs,
                ),
                [replace(entry, message=ModelRequest([UserPromptPart("changed")]))],
            )
        )
    assert len(published) == 1
    async with runner_database.sessions.begin() as db:
        row = await db.get(AgentHistoryRow, (session_id, 10))
        assert row.kind == "response" and row.created_at == created_at
        assert row.message == {
            "parts": ModelMessagesTypeAdapter.dump_python([response], mode="json")[0]["parts"]
        }
        stored = await HistoryRepository(db).read_history_entries(session_id)
        assert stored[0].seq == 10 and stored[0].message.metadata == response.metadata
        assert isinstance(stored[0].message, ModelResponse)
        assert stored[0].message.usage.input_tokens == 100
        assert stored[0].message.usage.output_tokens == 20
        assert await HistoryRepository(db).read_history_entries(session_id, after_seq=10) == ()
        assert await SessionRepository(db).read_runner_state(session_id) == (None, 0)


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
    valid = HistoryMessage(session_id, 0, ModelRequest(parts=[UserPromptPart("valid")]))
    other = replace(valid, seq=1, **{k: v for k, v in invalid.items() if k != "seq"})
    if "seq" in invalid:
        other = replace(other, seq=invalid["seq"])
    data = (
        RunnerDeps(
            session_id=session_id,
            session_factory=runner_database.sessions,
            output_service=runner_database.outputs,
        ),
        [valid, other],
    )
    with pytest.raises(ValueError):
        await Recorder().persist_messages(*data)
    assert published == []
    async with runner_database.sessions.begin() as db:
        assert await HistoryRepository(db).read_history_entries(session_id) == ()


def history(session_id, seq, content="message"):
    return HistoryMessage(session_id, seq, ModelRequest([UserPromptPart(content)]))


@pytest.mark.integration
async def test_boundary_filters_in_input_order_and_seals_old_gaps(runner_database, monkeypatch):
    session_id = runner_database.session_id
    initial = [history(session_id, 0), history(session_id, 10)]
    await Recorder().persist_messages(
        *(
            RunnerDeps(
                session_id=session_id,
                session_factory=runner_database.sessions,
                output_service=runner_database.outputs,
            ),
            initial,
        )
    )
    async with runner_database.sessions.begin() as db:
        created_at = (await db.get(AgentHistoryRow, (session_id, 10))).created_at

    published = []

    @asynccontextmanager
    async def publisher(requested_session):
        assert requested_session == session_id
        async with runner_database.sessions.begin() as db:
            await db.execute(
                select(SessionRow.id)
                .where(col(SessionRow.id) == session_id)
                .with_for_update(nowait=True)
            )
            stored = await HistoryRepository(db).read_history_entries(session_id)
            assert [e.seq for e in stored] == [0, 10, 12, 13, 15, 16]

        async def publish(message):
            published.append(message)

        yield publish

    monkeypatch.setattr(runner_database.outputs, "publisher", publisher)
    batch = [
        history(session_id, 5, "old gap"),
        history(session_id, 10, "changed stored row"),
        history(session_id, 15),
        history(session_id, 12),
        history(session_id, 13),
        history(session_id, 16),
    ]
    await Recorder().persist_messages(
        *(
            RunnerDeps(
                session_id=session_id,
                session_factory=runner_database.sessions,
                output_service=runner_database.outputs,
            ),
            batch,
        )
    )
    assert published == [MessageCommitted(entry) for entry in batch[2:]]
    async with runner_database.sessions.begin() as db:
        stored = await HistoryRepository(db).read_history_entries(session_id)
        assert stored[:2] == tuple(initial)
        assert (await db.get(AgentHistoryRow, (session_id, 10))).created_at == created_at

    published.clear()
    await Recorder().persist_messages(
        *(
            RunnerDeps(
                session_id=session_id,
                session_factory=runner_database.sessions,
                output_service=runner_database.outputs,
            ),
            [
                history(session_id, 12, "changed retry"),
                history(session_id, 14, "sealed gap"),
            ],
        )
    )
    await Recorder().persist_messages(
        *(
            RunnerDeps(
                session_id=session_id,
                session_factory=runner_database.sessions,
                output_service=runner_database.outputs,
            ),
            [],
        )
    )
    assert published == []
    async with runner_database.sessions.begin() as db:
        assert await HistoryRepository(db).read_history_entries(session_id) == stored


@pytest.mark.integration
async def test_append_borrows_transaction_and_returns_accepted_inputs(runner_database):
    session_id = runner_database.session_id
    initial = [
        history(session_id, 0),
        history(session_id, 1),
        history(session_id, 4),
    ]
    async with runner_database.sessions.begin() as db:
        await lock_session(db, session_id)
        assert await HistoryRepository(db).append_history(session_id, initial) == tuple(initial)
    with pytest.raises(RuntimeError, match="rollback"):
        async with runner_database.sessions.begin() as db:
            await lock_session(db, session_id)
            entry = history(session_id, 7)
            assert await HistoryRepository(db).append_history(session_id, [entry]) == (entry,)
            assert [
                e.seq for e in await HistoryRepository(db).read_history_entries(session_id)
            ] == [
                0,
                1,
                4,
                7,
            ]
            raise RuntimeError("rollback")
    async with runner_database.sessions.begin() as db:
        assert await HistoryRepository(db).read_history_entries(session_id) == tuple(initial)


@pytest.mark.integration
async def test_record_missing_session_does_not_publish(runner_database, monkeypatch):
    session_id = uuid4()

    def unexpected_publisher(*args):
        pytest.fail("missing sessions must not publish")

    monkeypatch.setattr(runner_database.outputs, "publisher", unexpected_publisher)
    with pytest.raises(LookupError):
        await Recorder().persist_messages(
            *(
                RunnerDeps(
                    session_id=session_id,
                    session_factory=runner_database.sessions,
                    output_service=runner_database.outputs,
                ),
                [history(session_id, 0)],
            )
        )
    async with runner_database.sessions.begin() as db:
        assert await HistoryRepository(db).read_history_entries(session_id) == ()


@pytest.mark.integration
async def test_record_serializes_session_writes_before_reading_boundary(runner_database):
    session_id, other_id = runner_database.session_id, uuid4()
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
            await HistoryRepository(db).append_history(session_id, [history(session_id, 2)])
            event.listen(engine.sync_engine, "before_cursor_execute", before_execute)
            task = asyncio.create_task(
                Recorder().persist_messages(
                    *(
                        RunnerDeps(
                            session_id=session_id,
                            session_factory=runner_database.sessions,
                            output_service=runner_database.outputs,
                        ),
                        [history(session_id, 1)],
                    )
                )
            )
            async with asyncio.timeout(2):
                await attempted_lock.wait()
                # A different session must commit while the first writer holds its lock.
                await Recorder().persist_messages(
                    *(
                        RunnerDeps(
                            session_id=other_id,
                            session_factory=runner_database.sessions,
                            output_service=runner_database.outputs,
                        ),
                        [history(other_id, 1)],
                    )
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
        assert [e.seq for e in await HistoryRepository(db).read_history_entries(session_id)] == [2]
        assert [e.seq for e in await HistoryRepository(db).read_history_entries(other_id)] == [1]


@pytest.mark.integration
async def test_commit_before_broadcast_failure_recovers_without_retry_publication(
    runner_database, monkeypatch
):
    session_id, outputs = runner_database.session_id, runner_database.outputs
    sessions = SessionService(
        runner_database.sessions, output_service=outputs, live_poll_interval=0.02
    )
    first = history(session_id, 1)
    await Recorder().persist_messages(
        *(
            RunnerDeps(
                session_id=session_id,
                session_factory=runner_database.sessions,
                output_service=runner_database.outputs,
            ),
            [first],
        )
    )
    final = history(session_id, 7)
    data = (
        RunnerDeps(
            session_id=session_id,
            session_factory=runner_database.sessions,
            output_service=runner_database.outputs,
        ),
        [final],
    )

    def lose_broadcast(*args):
        raise RuntimeError("lost before broadcast")

    async with aclosing(sessions.live(session_id)) as batches:
        assert await anext(batches) == [MessageCommitted(first)]
        monkeypatch.setattr(outputs, "publisher", lose_broadcast)
        with pytest.raises(RuntimeError, match="lost before broadcast"):
            await Recorder().persist_messages(*data)
        # An acknowledged database write must not retry its failed publication.
        await Recorder().persist_messages(*data)
        async with asyncio.timeout(2):
            assert await anext(batches) == [MessageCommitted(final)]

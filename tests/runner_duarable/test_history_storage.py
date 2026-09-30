"""Real Temporal codecs, PostgreSQL overwrites and committed full-message broadcasts."""

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
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError

from kapy.agent_runner import HistoryMessage, MessageCommitted
from kapy.agent_runner.models import AgentHistoryRow, AgentStateRow
from kapy.agent_runner.repository import AgentRepository
from kapy.control.sessions.repository import SessionRepository
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
async def test_overwrite_columns_and_broadcast_complete_input_after_commit(
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
    entry = HistoryMessage(session_id, 10, True, response)
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
            assert row is not None and row.authoritative
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


@pytest.mark.integration
async def test_upsert_borrows_transaction_and_keeps_uncovered_rows(runner_database):
    session_id = runner_database.session_id
    entry = HistoryMessage(session_id, 8, True, ModelRequest(parts=[UserPromptPart("kept")]))
    async with runner_database.sessions.begin() as db:
        await AgentRepository(db).upsert_history(session_id, [entry])
    with pytest.raises(RuntimeError, match="rollback"):
        async with runner_database.sessions.begin() as db:
            await AgentRepository(db).upsert_history(
                session_id,
                [
                    replace(
                        entry,
                        authoritative=False,
                        message=ModelResponse(parts=[TextPart("rolled back")]),
                    )
                ],
            )
            raise RuntimeError("rollback")
    async with runner_database.sessions.begin() as db:
        await AgentRepository(db).upsert_history(session_id, [replace(entry, seq=3)])
        entries = await AgentRepository(db).read_history_entries(session_id)
        assert [e.seq for e in entries] == [3, 8] and entries[-1] == entry

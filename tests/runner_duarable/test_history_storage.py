"""Temporal payloads and real PostgreSQL overwrites preserve the history contract."""

from copy import deepcopy
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

from kapy.agent_runner.models import AgentHistoryRow, AgentStateRow
from kapy.agent_runner.repository import AgentRepository
from kapy.control.sessions.repository import SessionRepository
from kapy.runner_duarable.activities import RunnerStateActivities
from kapy.runner_duarable.types import RecordHistoryInput

pytestmark = pytest.mark.asyncio


async def test_message_payload_round_trip_uses_sdk_binary_codec():
    messages = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    ["image", BinaryContent(data=b"\xff\x00\xfe", media_type="image/png")]
                )
            ],
            metadata={"seq": 4, "source": "upload"},
        ),
        ModelResponse(parts=[TextPart("answer")], metadata={"seq": 5}),
    ]
    data = RecordHistoryInput(session_id=uuid4(), messages=messages)
    converter = DataConverter(payload_converter_class=PydanticAIPayloadConverter)
    restored = (await converter.decode(await converter.encode([data]), [RecordHistoryInput]))[0]
    expected = ModelMessagesTypeAdapter.validate_json(ModelMessagesTypeAdapter.dump_json(messages))
    assert restored.session_id == data.session_id and restored.messages == expected
    image = restored.messages[0].parts[0].content[1]
    assert image.data == b"\xff\x00\xfe"


@pytest.mark.integration
async def test_upsert_overwrites_all_columns_preserves_creation_and_checkpoint(runner_database):
    session_id = runner_database.session_id
    activities = RunnerStateActivities(runner_database.sessions)
    response = ModelResponse(
        parts=[TextPart("old")],
        metadata={"seq": 10, "old_key": "old"},
        finish_reason="stop",
        usage=RequestUsage(input_tokens=100, output_tokens=20),
    )
    first = RecordHistoryInput(session_id=session_id, messages=[response])
    await activities.record_history(first)
    async with runner_database.sessions.begin() as db:
        row = await db.get(AgentHistoryRow, (session_id, 10))
        assert row is not None
        created_at = row.created_at
        assert (row.finish_reason, row.input_tokens, row.output_tokens) == ("stop", 100, 20)
    await activities.record_history(first)
    replacement = ModelRequest(
        parts=[UserPromptPart(["new", BinaryContent(data=b"\xff", media_type="image/png")])],
        metadata={"seq": 10, "new_key": "new"},
        instructions="updated instructions",
    )
    updated = RecordHistoryInput(session_id=session_id, messages=[replacement])
    await activities.record_history(updated)
    async with runner_database.sessions.begin() as db:
        row = await db.get(AgentHistoryRow, (session_id, 10))
        assert row is not None and row.created_at == created_at
        assert row.kind == "request"
        assert (row.finish_reason, row.input_tokens, row.output_tokens) == (None, None, None)
        entries = await AgentRepository(db).read_history_entries(session_id)
        expected = ModelMessagesTypeAdapter.validate_json(
            ModelMessagesTypeAdapter.dump_json([replacement])
        )[0]
        assert len(entries) == 1 and entries[0].message == expected
        assert await AgentRepository(db).read_history_entries(session_id, after_seq=10) == ()
        assert await db.get(AgentStateRow, session_id) is None
        assert await SessionRepository(db).read_runner_state(session_id) == (None, 0)

    # A missing usage observation must also clear a response's old normalized tokens.
    replacement_response = deepcopy(response)
    replacement_response.usage = RequestUsage()
    replacement_response.finish_reason = None
    await activities.record_history(first)
    await activities.record_history(
        RecordHistoryInput(session_id=session_id, messages=[replacement_response])
    )
    async with runner_database.sessions.begin() as db:
        row = await db.get(AgentHistoryRow, (session_id, 10))
        assert row is not None and row.kind == "response" and row.created_at == created_at
        assert (row.finish_reason, row.input_tokens, row.output_tokens) == (None, None, None)


@pytest.mark.integration
@pytest.mark.parametrize("seq", [None, True, -1, "1", 1.5, 0])
async def test_invalid_batch_fails_non_retryably_without_partial_write(runner_database, seq):
    activities = RunnerStateActivities(runner_database.sessions)
    data = RecordHistoryInput(
        session_id=runner_database.session_id,
        messages=[
            ModelRequest(parts=[UserPromptPart("valid")], metadata={"seq": 0}),
            ModelResponse(parts=[TextPart("invalid or duplicate")], metadata={"seq": seq}),
        ],
    )
    with pytest.raises(ApplicationError) as error:
        await activities.record_history(data)
    assert error.value.type == "InvalidHistory" and error.value.non_retryable
    async with runner_database.sessions.begin() as db:
        assert await AgentRepository(db).read_history_entries(runner_database.session_id) == ()


@pytest.mark.integration
async def test_upsert_borrows_transaction_and_does_not_delete_uncovered_rows(runner_database):
    session_id = runner_database.session_id
    message = ModelRequest(parts=[UserPromptPart("kept")], metadata={"seq": 8})
    async with runner_database.sessions.begin() as db:
        await AgentRepository(db).upsert_history(session_id, [message])
    with pytest.raises(RuntimeError, match="rollback"):
        async with runner_database.sessions.begin() as db:
            await AgentRepository(db).upsert_history(
                session_id, [ModelResponse(parts=[TextPart("rolled back")], metadata={"seq": 8})]
            )
            raise RuntimeError("rollback")
    async with runner_database.sessions.begin() as db:
        await AgentRepository(db).upsert_history(
            session_id, [ModelResponse(parts=[TextPart("other")], metadata={"seq": 3})]
        )
        entries = await AgentRepository(db).read_history_entries(session_id)
        assert [entry.seq for entry in entries] == [3, 8]
        assert entries[-1].message == message

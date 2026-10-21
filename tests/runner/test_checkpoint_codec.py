"""Stored envelopes and legacy admission use complete immutable codec projections."""

import json
from copy import deepcopy
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

from kapy.agent_history import HistoryMessage
from kapy.control.sessions import SessionBusy
from kapy.runner.checkpoints import (
    Checkpoint,
    DoneNext,
    ModelRequestNext,
    decode_state,
    message_boundary,
    require_new_prompt,
    validate_history,
)


def messages():
    return [
        ModelRequest([UserPromptPart("input")], metadata={"seq": 5}, run_id="old-sdk-run"),
        ModelResponse(
            [TextPart("answer")],
            metadata={"seq": 10},
            usage=RequestUsage(input_tokens=100, output_tokens=20, cache_read_tokens=50),
        ),
    ]


def test_minimal_envelope_round_trip_preserves_native_sdk_fields_and_gaps():
    saved = Checkpoint(
        checkpoint="run_completed", messages=messages(), next=DoneNext(output="final output")
    )
    payload = json.loads(saved.model_dump_json())
    assert set(payload) == {"version", "checkpoint", "messages", "next"}
    assert decode_state(saved.model_dump_json()) == saved
    assert message_boundary(saved.messages) == 10
    assert message_boundary([]) == -1
    require_new_prompt(saved)


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"version": True},
        {"checkpoint": "unknown"},
        {"next": {"kind": "finish", "output": "premature"}},
        {"history_cursor": 10},
        {"turn": 1},
        {"conversation_id": "outer"},
        {"messages": [], "next": {"kind": "model_request"}},
    ],
)
def test_unknown_or_mismatched_envelopes_are_rejected(change):
    payload = json.loads(
        Checkpoint(
            checkpoint="run_completed", messages=messages(), next=DoneNext(output="done")
        ).model_dump_json()
    )
    with pytest.raises(ValueError):
        decode_state(json.dumps(payload | change))


@pytest.mark.parametrize("seq", [True, -1, 1.5, "1", None])
def test_invalid_message_positions_are_rejected(seq):
    payload = ModelMessagesTypeAdapter.dump_python(messages(), mode="json")
    payload[0]["metadata"]["seq"] = seq
    with pytest.raises(ValueError):
        decode_state(json.dumps(payload))


def test_legacy_allows_only_an_unnumbered_tail_and_never_implies_completion():
    legacy = messages() + [ModelRequest([UserPromptPart("old after-run tail")])]
    saved = decode_state(ModelMessagesTypeAdapter.dump_json(legacy).decode())
    assert isinstance(saved, list)
    session_id = uuid4()
    entries = [HistoryMessage(session_id, seq, m) for seq, m in zip([5, 10], legacy, strict=False)]
    validate_history(saved, entries)
    require_new_prompt(saved)
    with pytest.raises(ValueError, match="suffix"):
        decode_state(
            ModelMessagesTypeAdapter.dump_json([legacy[0], legacy[-1], legacy[1]]).decode()
        )
    unfinished = Checkpoint(
        checkpoint="before_model_request", messages=[legacy[0]], next=ModelRequestNext()
    )
    with pytest.raises(SessionBusy):
        require_new_prompt(unfinished)


@pytest.mark.parametrize("mismatch", ["position", "content", "ahead", "null"])
def test_history_positions_and_codec_content_must_correspond(mismatch):
    data, session_id = messages(), uuid4()
    stored = deepcopy(data)
    assert isinstance(stored[1], ModelResponse)
    stored[1].usage = RequestUsage(input_tokens=100, output_tokens=20)
    entries = [HistoryMessage(session_id, seq, m) for seq, m in zip([5, 10], stored, strict=True)]
    state = Checkpoint(checkpoint="run_completed", messages=data, next=DoneNext(output="done"))
    validate_history(state, entries)  # Full SDK usage and normalized usage match.
    if mismatch == "position":
        entries[0] = HistoryMessage(session_id, 4, stored[0])
    elif mismatch == "content":
        stored[1].parts = [TextPart("changed")]
    elif mismatch == "ahead":
        entries.append(HistoryMessage(session_id, 11, ModelRequest([])))
    else:
        state = None
    with pytest.raises(ValueError):
        validate_history(state, entries)

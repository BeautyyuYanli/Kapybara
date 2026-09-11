"""Replay uses complete tool chains and a fixed anchor, independent of DB page size."""

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from kapy.tmpv2.agent_runner.compaction import replay_start


@pytest.mark.parametrize("page_size", [1, 2, 3, 5, 64])
@pytest.mark.parametrize("turns, expected_start", [(1, 2), (2, 2), (3, 0), (10, 0)])
def test_replay_rounds_back_through_parallel_calls_retries_and_continuous_requests(
    page_size, turns, expected_start
):
    history = [
        ModelRequest(parts=[UserPromptPart("older")]),
        ModelResponse(parts=[TextPart("older answer")]),
        ModelRequest(parts=[UserPromptPart("start chain")]),
        ModelRequest(parts=[UserPromptPart("steer")]),
        ModelResponse(parts=[ToolCallPart("a", {}, "a-id"), ToolCallPart("b", {}, "b-id")]),
        ModelRequest(
            parts=[
                ToolReturnPart("a", "a", "a-id"),
                RetryPromptPart("retry", tool_name="b", tool_call_id="b-id"),
            ]
        ),
        ModelResponse(parts=[ToolCallPart("b", {}, "b2-id")]),
        ModelRequest(parts=[ToolReturnPart("b", "b", "b2-id")]),
    ]
    rows = list(enumerate(history))
    loaded = []
    before = len(rows)
    while True:
        loaded[:0] = rows[max(0, before - page_size) : before]
        start = replay_start(loaded, turns)
        if start is not None:
            break
        before = loaded[0][0]
        assert before > 0, "complete history must resolve its replay window"
    assert start == expected_start
    assert [seq for seq, _ in loaded if seq >= start] == list(range(expected_start, len(history)))


def test_replay_keeps_request_only_history_when_no_response_exists():
    rows = list(
        enumerate(
            [
                ModelRequest(parts=[UserPromptPart("one")]),
                ModelRequest(parts=[UserPromptPart("two")]),
            ]
        )
    )
    assert replay_start(rows[1:], 1) is None
    assert replay_start(rows, 1) == 0

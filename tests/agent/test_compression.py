import copy
from typing import Any

from pydantic_ai.messages import ModelMessagesTypeAdapter

from kapy.agent.compression import projection, sweep, usage_sweep


def cycle(index: int, *, closed: bool = True) -> dict[str, Any]:
    return {
        "turn_id": str(index),
        "closed": closed,
        "level": 0,
        "inputs": [f"input {index}"],
        "outputs": [f"answer {index}"],
        "messages": [
            {
                "kind": "request",
                "parts": [{"part_kind": "user-prompt", "content": f"input {index}"}],
            },
            {
                "kind": "response",
                "parts": [
                    {
                        "part_kind": "tool-call",
                        "tool_name": "one",
                        "args": {"exact": [1, 2]},
                        "tool_call_id": f"{index}-a",
                    },
                    {
                        "part_kind": "tool-call",
                        "tool_name": "two",
                        "args": {},
                        "tool_call_id": f"{index}-b",
                    },
                ],
            },
            {
                "kind": "request",
                "parts": [
                    {
                        "part_kind": "tool-return",
                        "tool_name": "one",
                        "content": "valuable result",
                        "tool_call_id": f"{index}-a",
                        "metadata": {"kapy_media_refs": ["reference"]},
                    },
                    {
                        "part_kind": "tool-return",
                        "tool_name": "two",
                        "content": "second result",
                        "tool_call_id": f"{index}-b",
                    },
                ],
            },
            {"kind": "response", "parts": [{"part_kind": "text", "content": f"answer {index}"}]},
        ],
    }


def paired(data: dict[str, Any]) -> None:
    calls = set()
    returns = set()
    messages = projection(data)
    ModelMessagesTypeAdapter.validate_python(messages)
    for message in messages:
        for part in message["parts"]:
            if part["part_kind"] == "tool-call":
                assert part["tool_call_id"] not in calls
                calls.add(part["tool_call_id"])
            if part["part_kind"] == "tool-return":
                assert part["tool_call_id"] not in returns
                returns.add(part["tool_call_id"])
    assert calls == returns


def test_fresh_usage_once_and_layers_keep_pairing_and_recent_cycle() -> None:
    data = {
        "cycles": [cycle(i) for i in range(10)],
        "media_fallback_call_ids": [],
        "last_usage": {
            "response_id": "response",
            "model": "test",
            "input_tokens": 690,
            "output_tokens": 10,
            "cache_read_tokens": 600,
            "sweep_applied": False,
        },
    }
    newest = copy.deepcopy(data["cycles"][-1])
    assert usage_sweep(data, "test", 1000, 0.70, 0.10)
    paired(data)
    once = copy.deepcopy(data)
    assert not usage_sweep(data, "test", 1000, 0.70, 0.10)
    assert data == once
    assert data["cycles"][-1] == newest
    assert data["cycles"][0]["level"] == 1
    assert "valuable result" not in str(data["cycles"][0])
    assert "kapy_media_refs" not in str(data["cycles"][0])
    assert sweep(data, 0.10)
    assert data["cycles"][0]["level"] == 2
    assert "tool-call" not in str(data["cycles"][0])
    paired(data)
    assert sweep(data, 0.10)
    assert len(data["cycles"]) < 10
    paired(data)


def test_usage_unknown_model_change_and_cached_tokens_do_not_trigger_sweep() -> None:
    data: dict[str, Any] = {"cycles": [cycle(1), cycle(2)], "last_usage": None}
    assert not usage_sweep(data, "test", 1000, 0.70, 0.10)
    data["last_usage"] = {
        "model": "test",
        "input_tokens": 600,
        "output_tokens": 10,
        "cache_read_tokens": 500,
        "sweep_applied": False,
    }
    assert not usage_sweep(data, "test", 1000, 0.70, 0.10)
    assert not usage_sweep(data, "another-model", 1000, 0.50, 0.10)


def test_open_cycle_only_omits_older_completed_blocks() -> None:
    old = cycle(1, closed=False)
    old["messages"].extend(cycle(2)["messages"])
    old["messages"].append(
        {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "new steer"}]}
    )
    data = {"cycles": [old]}
    assert sweep(data, 0.10)
    assert old["level"] == 0
    assert "new steer" in str(old["messages"][-1])
    paired(data)


def test_every_partial_reply_survives_both_compression_levels() -> None:
    old = cycle(1)
    outputs = [
        {"kind": "reply_to", "being_waited_ids": [f"address-{i}"], "payload": f"body-{i}"}
        for i in range(2)
    ]
    old["outputs"] = outputs
    for i, part in enumerate(old["messages"][2]["parts"]):
        part["tool_name"] = "reply_to"
        old["messages"][1]["parts"][i]["tool_name"] = "reply_to"
        part["content"] = {"output": outputs[i], "remaining_being_waited_ids": []}
    data = {"cycles": [old, cycle(2)]}
    sweep(data, 0.1)
    assert [p["content"]["output"] for p in old["messages"][2]["parts"]] == outputs
    sweep(data, 0.1)
    import json

    assert [
        json.loads(m["parts"][0]["content"]) for m in old["messages"] if m["kind"] == "response"
    ] == outputs
    paired(data)

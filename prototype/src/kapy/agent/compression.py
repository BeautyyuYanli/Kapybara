"""One-step degradation driven only by fresh provider usage observations."""

import copy
import json
import math
from typing import Any


def interaction_blocks(messages: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    blocks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    pending: set[str] = set()
    response_seen = False
    for message in messages:
        if current and response_seen and not pending:
            blocks.append(current)
            current = []
            response_seen = False
        current.append(message)
        for part in message["parts"]:
            kind = part["part_kind"]
            if kind == "tool-call":
                pending.add(part["tool_call_id"])
            elif kind in ("tool-return", "retry-prompt") and part.get("tool_call_id"):
                pending.discard(part["tool_call_id"])
        response_seen |= message["kind"] == "response"
    if current:
        blocks.append(current)
    return blocks


def omit_results(messages: list[dict[str, Any]]) -> bool:
    changed = False
    for message in messages:
        for part in message["parts"]:
            if (
                part["part_kind"] == "tool-return"
                and part.get("tool_name") != "reply_to"
                and (part.get("metadata") or {}).get("kapy_compression") != 1
            ):
                part["content"] = (
                    "[Earlier tool result omitted. Query session history for details.]"
                )
                metadata = part.get("metadata") or {}
                metadata.pop("kapy_media_refs", None)
                metadata["kapy_compression"] = 1
                part["metadata"] = metadata
                changed = True
    return changed


def sweep(data: dict[str, Any], keep_recent_ratio: float) -> bool:
    """Advance eligible old cycles exactly one level, keeping complete tool batches."""
    cycles = data["cycles"]
    blocks = [interaction_blocks(cycle["messages"]) for cycle in cycles]
    total = sum(len(group) for group in blocks)
    protected_from = max(0, total - max(1, math.ceil(total * keep_recent_ratio)))
    offset = 0
    changed = False
    retained = []
    for cycle, group in zip(cycles, blocks, strict=True):
        end = offset + len(group)
        if cycle["closed"]:
            if end > protected_from or cycle.get("unreplied", False):
                retained.append(cycle)
            elif cycle["level"] == 0:
                omit_results(cycle["messages"])
                cycle["level"] = 1
                retained.append(cycle)
                changed = True
            elif cycle["level"] == 1:
                inputs = cycle.get("inputs", [])
                cycle["messages"] = [
                    {"kind": "request", "parts": [{"part_kind": "user-prompt", "content": value}]}
                    for value in inputs
                ] + [
                    {
                        "kind": "response",
                        "parts": [
                            {
                                "part_kind": "text",
                                "content": (
                                    output
                                    if isinstance(output, str)
                                    else json.dumps(output, ensure_ascii=False)
                                ),
                            }
                        ],
                    }
                    for output in cycle["outputs"]
                ]
                cycle["level"] = 2
                retained.append(cycle)
                changed = True
            else:
                changed = True
        else:
            for index, block in enumerate(group):
                # A trailing request or incomplete tool batch is never downgraded.
                has_response = any(m["kind"] == "response" for m in block)
                calls = {
                    p["tool_call_id"]
                    for m in block
                    for p in m["parts"]
                    if p["part_kind"] == "tool-call"
                }
                returns = {
                    p.get("tool_call_id")
                    for m in block
                    for p in m["parts"]
                    if p["part_kind"] in ("tool-return", "retry-prompt")
                }
                if offset + index < protected_from and has_response and calls <= returns:
                    changed |= omit_results(block)
            retained.append(cycle)
        offset = end
    data["cycles"] = retained
    visible_ids = {
        p.get("tool_call_id")
        for c in retained
        for m in c["messages"]
        for p in m["parts"]
        if p["part_kind"] == "tool-return"
    }
    data["media_fallback_call_ids"] = [
        i for i in data.get("media_fallback_call_ids", []) if i in visible_ids
    ]
    return changed


def usage_sweep(data: dict[str, Any], model: str, window: int, ratio: float, keep: float) -> bool:
    usage = data.get("last_usage")
    if not usage or usage["model"] != model or usage["sweep_applied"]:
        return False
    if usage["input_tokens"] + usage["output_tokens"] < window * ratio:
        return False
    sweep(data, keep)
    usage["sweep_applied"] = True
    return True


def projection(data: dict[str, Any]) -> list[dict[str, Any]]:
    return copy.deepcopy([m for cycle in data["cycles"] for m in cycle["messages"]])

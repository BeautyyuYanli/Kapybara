"""Verify a real parent -> CLI child -> waiting event -> parent completion."""

import argparse
import asyncio
import json
import os
import re
import shlex
import time
from typing import Any
from uuid import uuid4

import httpx2


async def check(machine_id: str, model_id: str) -> None:
    base = os.environ.get("KAPY_CONTROL_URL", "http://127.0.0.1:8000").rstrip("/")
    label = "Kapy recursive acceptance " + uuid4().hex[:12]
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + os.environ["KAPY_CONTROL_TOKEN"]}, timeout=40
    ) as client:

        async def rpc(method: str, params: dict[str, Any]) -> Any:
            identity = uuid4().hex
            response = await client.post(
                base + "/rpc",
                json={"jsonrpc": "2.0", "id": identity, "method": method, "params": params},
            )
            response.raise_for_status()
            body = response.json()
            if body.get("id") != identity:
                raise AssertionError("Response ID mismatch")
            if "error" in body:
                raise AssertionError(f"{method} failed: {body['error'].get('code')}")
            return body["result"]

        parent = await rpc(
            "session.create",
            {
                "request_id": str(uuid4()),
                "title": label + " parent",
                "machine_ids": [machine_id],
                "default_machine_id": machine_id,
                "config": {"model": {"model_id": model_id}},
            },
        )
        parent_id = parent["session"]["id"]
        started = time.perf_counter()
        try:
            child_prompt = (
                "Use a process tool to execute exactly once:\n"
                "python -c 'import secrets; print(\"KAPY_CHILD_\" + secrets.token_hex(16))'\n"
                "Reply with only the random marker actually printed by the process."
            )
            command = shlex.join(
                [
                    "kapy",
                    "control",
                    "session",
                    "create",
                    child_prompt,
                    "--title",
                    label + " child",
                    "--machine",
                    machine_id,
                ]
            )
            await rpc(
                "session.input",
                {
                    "session_id": parent_id,
                    "request_id": str(uuid4()),
                    "mode": "queue",
                    "payload": (
                        "Create one child task by executing this exact command with process_start "
                        f"in stdio mode on the default machine:\n{command}\n"
                        "After successful creation, read waiting_id from the CLI JSON receipt "
                        "and use your wait tool with wait_for containing only that ID. "
                        "Do not poll the child or wait via CLI. "
                        "When its completion event arrives, reply with only the child's random "
                        "KAPY_CHILD_ marker from that event. Do not generate a marker yourself."
                    ),
                },
            )
            records: list[dict[str, Any]] = []
            cursor = None
            marker = None
            async with asyncio.timeout(240):
                while marker is None:
                    page = await rpc(
                        "session.output",
                        {"session_id": parent_id, "after": cursor, "wait_seconds": 30},
                    )
                    records.extend(page["items"])
                    cursor = page["next_cursor"]
                    for record in page["items"]:
                        if record["kind"] == "error":
                            raise AssertionError("Recursive parent run failed; inspect its history")
                        text = record.get("text", "").strip()
                        if record["kind"] == "final" and text:
                            if not re.fullmatch(r"KAPY_CHILD_[0-9a-f]{32}", text):
                                raise AssertionError(
                                    f"Parent completed without the child marker: {text[:2000]}"
                                )
                            marker = text
            events = [
                record
                for record in records
                if record["kind"] == "input"
                and isinstance(record["data"], dict)
                and record["data"].get("type") == "event"
            ]
            if len(events) != 1 or marker not in json.dumps(events[0]["data"]):
                raise AssertionError(
                    "Parent reply did not match exactly one child completion event"
                )
            child_id = events[0]["data"]["producer_session_id"]
            child = await rpc("session.get", {"session_id": child_id})
            if child["title"] != label + " child":
                raise AssertionError("Completion came from the wrong child")
            async with asyncio.timeout(30):
                while len({r["run_id"] for r in records if r["kind"] == "waiting"}) < 2:
                    page = await rpc(
                        "session.output",
                        {"session_id": parent_id, "after": cursor, "wait_seconds": 10},
                    )
                    records.extend(page["items"])
                    cursor = page["next_cursor"]
            print(
                json.dumps(
                    {
                        "parent_session_id": parent_id,
                        "child_session_id": child_id,
                        "cli_creation": True,
                        "child_completion_events": len(events),
                        "parent_waited_and_resumed": True,
                        "random_marker_matches_event": True,
                        "seconds": round(time.perf_counter() - started, 2),
                    }
                )
            )
        finally:
            page = await rpc("session.list", {"limit": 200})
            temporary = [s for s in page["items"] if s["title"].startswith(label)]
            for session in sorted(temporary, key=lambda item: item["id"] == parent_id):
                await rpc(
                    "session.delete", {"session_id": session["id"], "request_id": str(uuid4())}
                )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--model-id", required=True)
    args = parser.parse_args()
    asyncio.run(check(args.machine, args.model_id))

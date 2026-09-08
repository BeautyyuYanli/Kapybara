"""Exercise a running control plane and Docker machine with the configured model.

This creates and deletes one temporary session. The machine creates an unpredictable
marker; success requires both its durable tool result and the final model reply.
"""

import argparse
import asyncio
import json
import os
import re
import time
from typing import Any
from uuid import uuid4

import httpx2


async def check(machine_id: str, model_id: str) -> None:
    base_url = os.environ.get("KAPY_CONTROL_URL", "http://127.0.0.1:8000").rstrip("/")
    token = os.environ["KAPY_CONTROL_TOKEN"]
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=40
    ) as client:

        async def rpc(method: str, params: dict[str, Any]) -> Any:
            envelope_id = uuid4().hex
            response = await client.post(
                base_url + "/rpc",
                json={"jsonrpc": "2.0", "id": envelope_id, "method": method, "params": params},
            )
            response.raise_for_status()
            body = response.json()
            if body.get("id") != envelope_id or body.get("jsonrpc") != "2.0":
                raise AssertionError("Invalid RPC response envelope")
            if "error" in body:
                # Error bodies may include user data. Report only the method and code.
                raise AssertionError(f"{method} failed: code {body['error'].get('code')}")
            return body["result"]

        params = {
            "request_id": str(uuid4()),
            "title": "Kapy live acceptance " + uuid4().hex[:8],
            "machine_ids": [machine_id],
            "default_machine_id": machine_id,
            "config": {"model": {"model_id": model_id}},
        }
        created = await rpc("session.create", params)
        session_id = created["session"]["id"]
        try:
            replay = await rpc("session.create", params)
            if replay["session"]["id"] != session_id:
                raise AssertionError("Repeated creation produced a different session")

            command = "python -c 'import secrets; print(\"KAPY_MACHINE_\" + secrets.token_hex(16))'"
            request_id = str(uuid4())
            started = time.perf_counter()
            submission = await rpc(
                "session.input",
                {
                    "session_id": session_id,
                    "request_id": request_id,
                    "mode": "queue",
                    "payload": (
                        "Use the process tool on the default machine to execute this command "
                        f"exactly once: {command}\n"
                        "Wait for the command to finish if necessary. "
                        "Reply with exactly the random marker printed by that command."
                    ),
                },
            )
            accepted_seconds = time.perf_counter() - started
            if submission["request_id"] != request_id:
                raise AssertionError("Input receipt lost its request ID")

            async with asyncio.timeout(180):
                while True:
                    status = await rpc(
                        "session.wait",
                        {"session_id": session_id, "request_id": request_id, "wait_seconds": 30},
                    )
                    completion = status["completion"]
                    if completion is not None:
                        break
            marker = completion["output"].strip()
            if not re.fullmatch(r"KAPY_MACHINE_[0-9a-f]{32}", marker):
                raise AssertionError("Final output did not contain the machine-generated marker")

            cursor = None
            tool_result_found = False
            history_records = 0
            async with asyncio.timeout(30):
                while True:
                    page = await rpc(
                        "history.read", {"session_id": session_id, "after": cursor, "limit": 200}
                    )
                    for record in page["items"]:
                        history_records += 1
                        data = record["data"]
                        if record["kind"] == "tool_result" and marker in json.dumps(data):
                            tool_result_found = True
                        if record["kind"] == "model_request" and isinstance(data, dict):
                            for part in data.get("parts", []):
                                if part.get("part_kind") == "tool-return" and marker in json.dumps(
                                    part
                                ):
                                    tool_result_found = True
                    cursor = page["next_cursor"]
                    if not page["has_more"]:
                        break
            if not tool_result_found:
                raise AssertionError("Marker was absent from durable tool results")
            print(
                json.dumps(
                    {
                        "session_id": session_id,
                        "creation_replay_stable": True,
                        "machine_tool_result_matches_reply": True,
                        "history_records": history_records,
                        "input_accept_ms": round(accepted_seconds * 1000, 2),
                        "completion_seconds": round(time.perf_counter() - started, 2),
                    }
                )
            )
        finally:
            await rpc("session.delete", {"session_id": session_id, "request_id": str(uuid4())})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine", required=True, help="An online Docker execution machine ID")
    parser.add_argument("--model-id", required=True, help="Registered model UUID")
    arguments = parser.parse_args()
    asyncio.run(check(arguments.machine, arguments.model_id))

"""Check concurrent output observers against a running control plane and live model."""

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import time
from typing import Any
from uuid import uuid4

import httpx2


async def benchmark(machine: str, sessions: int, observers: int) -> None:
    base = os.environ.get("KAPY_CONTROL_URL", "http://127.0.0.1:8000").rstrip("/")
    latencies: list[float] = []
    created: list[tuple[str, str]] = []
    tasks: list[asyncio.Task] = []
    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + os.environ["KAPY_CONTROL_TOKEN"]},
        timeout=40,
        limits=httpx2.Limits(max_connections=128, max_keepalive_connections=128),
    ) as client:

        async def rpc(method: str, **params: Any) -> Any:
            identity = uuid4().hex
            response = await client.post(
                base + "/rpc",
                json={"jsonrpc": "2.0", "id": identity, "method": method, "params": params},
            )
            response.raise_for_status()
            body = response.json()
            if body.get("id") != identity or "error" in body:
                raise AssertionError(f"{method} failed or returned a mismatched response ID")
            return body["result"]

        async def observe(session: str, marker: str) -> tuple[str, str, int]:
            cursor = None
            records = []
            seen: set[str] = set()
            while True:
                page = await rpc(
                    "session.output", session_id=session, after=cursor, wait_seconds=10
                )
                for record in page["items"]:
                    if record["cursor"] in seen:
                        raise AssertionError("An output cursor was repeated")
                    seen.add(record["cursor"])
                    records.append(record)
                    if record["kind"] == "error":
                        raise AssertionError("The observed model run failed")
                    if record["kind"] == "final":
                        if record["text"].strip() != marker:
                            raise AssertionError("Final output crossed sessions or lost content")
                        fingerprint = hashlib.sha256(
                            json.dumps(records, sort_keys=True).encode()
                        ).hexdigest()
                        return session, fingerprint, len(records)
                cursor = page["next_cursor"]

        try:
            for _ in range(sessions):
                marker = "KAPY_POLL_" + uuid4().hex
                result = await rpc(
                    "session.create",
                    request_id=str(uuid4()),
                    title=marker,
                    machine_ids=[machine],
                    default_machine_id=machine,
                    config={},
                )
                created.append((result["session"]["id"], marker))
            started = time.perf_counter()
            async with asyncio.timeout(180):
                tasks = [
                    asyncio.create_task(observe(session, marker))
                    for session, marker in created
                    for _ in range(observers)
                ]

                async def submit(session: str, marker: str) -> None:
                    before = time.perf_counter()
                    await rpc(
                        "session.input",
                        session_id=session,
                        request_id=str(uuid4()),
                        mode="queue",
                        payload="Reply with exactly this text: " + marker,
                    )
                    latencies.append((time.perf_counter() - before) * 1000)

                await asyncio.gather(*(submit(session, marker) for session, marker in created))
                results = await asyncio.gather(*tasks)
            for session, _ in created:
                reads = [(digest, count) for sid, digest, count in results if sid == session]
                if len(reads) != observers or len(set(reads)) != 1:
                    raise AssertionError("Concurrent observers did not replay identical output")
            print(
                json.dumps(
                    {
                        "sessions": sessions,
                        "observers": len(results),
                        "errors": 0,
                        "per_session_replay_identical": True,
                        "input_accept_median_ms": round(statistics.median(latencies), 2),
                        "input_accept_max_ms": round(max(latencies), 2),
                        "completion_seconds": round(time.perf_counter() - started, 2),
                        "records_per_observer": sorted({count for _, _, count in results}),
                    }
                )
            )
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for session, _ in created:
                await rpc("session.delete", session_id=session, request_id=str(uuid4()))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--sessions", type=int, default=4)
    parser.add_argument("--observers", type=int, default=25)
    args = parser.parse_args()
    if not 1 <= args.sessions <= 8 or not 1 <= args.observers <= 25:
        parser.error("Use 1–8 sessions and 1–25 observers per session")
    asyncio.run(benchmark(args.machine, args.sessions, args.observers))

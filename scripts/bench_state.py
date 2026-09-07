"""Exercise session ordering and durable receipts against local PostgreSQL/Valkey."""

import argparse
import asyncio
import json
import os
import statistics
import time
from collections import defaultdict
from uuid import UUID, uuid4

import psycopg
from psycopg import sql

from kapy.state import (
    CheckpointWrite,
    RunContext,
    RunnerState,
    RunResult,
    SessionService,
    SessionSpec,
)


async def benchmark(session_count: int, inputs_per_session: int) -> None:
    database_url = os.environ.get(
        "KAPY_DATABASE_URL", "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy"
    )
    valkey_url = os.environ.get("KAPY_VALKEY_URL", "redis://127.0.0.1:56379/0")
    schema = "kapy_acceptance_" + uuid4().hex
    observed: dict[UUID, list[int]] = defaultdict(list)
    active: set[UUID] = set()
    peak_concurrent = 0
    latencies: list[float] = []

    async def runner(context: RunContext) -> RunResult:
        nonlocal peak_concurrent
        session_id = context.session.id
        if session_id in active:
            raise AssertionError("Overlapping runs in one session")
        active.add(session_id)
        peak_concurrent = max(peak_concurrent, len(active))
        try:
            # A small I/O delay gives independently scheduled runs time to overlap.
            await asyncio.sleep(0.002)
            for item in context.inputs:
                if not isinstance(item.payload, dict):
                    raise AssertionError("Unexpected input envelope")
                sequence = item.payload["sequence"]
                if not isinstance(sequence, int):
                    raise AssertionError("Input sequence was not preserved")
                observed[session_id].append(sequence)
            return RunResult(
                output="accepted",
                wait_for=(),
                checkpoint=CheckpointWrite(
                    number=context.checkpoint_number + 1,
                    state=RunnerState(codec="acceptance-v1", data={}),
                    messages=(),
                    consumed_input_ids=tuple(item.id for item in context.inputs),
                ),
            )
        finally:
            active.remove(session_id)

    def service() -> SessionService:
        return SessionService(
            database_url=database_url,
            valkey_url=valkey_url,
            runner=runner,
            schema=schema,
            namespace=schema,
        )

    expected: dict[UUID, set[str]] = {}

    async def receipts(state: SessionService, session_id: UUID) -> set[str]:
        completed: set[str] = set()
        cursor = None
        while not expected[session_id].issubset(completed):
            page = await state.read_output(session_id, after=cursor, limit=200, wait_seconds=1)
            cursor = page.next_cursor
            for record in page.items:
                if record.kind == "error":
                    raise AssertionError("Runner failed during acceptance")
                if record.kind != "waiting" or not isinstance(record.data, dict):
                    continue
                request_ids = record.data.get("request_ids")
                if isinstance(request_ids, list):
                    completed.update(str(request_id) for request_id in request_ids)
        return completed & expected[session_id]

    try:
        async with service() as state:
            for index in range(session_count):
                created = await state.create_session(
                    SessionSpec(
                        title=f"acceptance-{index}",
                        machine_ids=(),
                        default_machine_id=None,
                        config={},
                        initial_state=RunnerState(codec="acceptance-v1", data={}),
                    ),
                    request_id=uuid4(),
                )
                expected[created.session.id] = set()

            async def submit(session_id: UUID) -> None:
                for sequence in range(inputs_per_session):
                    request_id = uuid4()
                    started = time.perf_counter()
                    await state.submit_input(
                        session_id,
                        {"sequence": sequence},
                        request_id=request_id,
                        mode="queue",
                    )
                    latencies.append(time.perf_counter() - started)
                    expected[session_id].add(str(request_id))

            started = time.perf_counter()
            async with asyncio.timeout(180):
                await asyncio.gather(*(submit(session_id) for session_id in expected))
                completed = await asyncio.gather(
                    *(receipts(state, session_id) for session_id in expected)
                )
            elapsed = time.perf_counter() - started

        wanted_order = list(range(inputs_per_session))
        if any(observed[session_id] != wanted_order for session_id in expected):
            raise AssertionError("Missing, duplicated or out-of-order session input")
        async with service() as reopened, asyncio.timeout(60):
            replayed = await asyncio.gather(
                *(receipts(reopened, session_id) for session_id in expected)
            )
        count = session_count * inputs_per_session
        p95 = sorted(latencies)[min(len(latencies) - 1, int(len(latencies) * 0.95))]
        print(
            json.dumps(
                {
                    "sessions": session_count,
                    "accepted": count,
                    "completed": sum(map(len, completed)),
                    "replayed_after_reopen": sum(map(len, replayed)),
                    "seconds": round(elapsed, 3),
                    "inputs_per_second": round(count / elapsed, 2),
                    "submit_p50_ms": round(statistics.median(latencies) * 1000, 2),
                    "submit_p95_ms": round(p95 * 1000, 2),
                    "peak_concurrent_sessions": peak_concurrent,
                    "ordering_preserved": True,
                }
            )
        )
    finally:
        async with await psycopg.AsyncConnection.connect(database_url) as connection:
            await connection.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=100)
    parser.add_argument("--inputs", type=int, default=20)
    args = parser.parse_args()
    if args.sessions < 1 or args.inputs < 1:
        parser.error("--sessions and --inputs must be positive")
    asyncio.run(benchmark(args.sessions, args.inputs))

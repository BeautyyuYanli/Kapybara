"""Measure durable broadcast, repeated wakeups and publish-before-subscribe."""

import argparse
import asyncio
import json
import os
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
    migrate,
)


async def benchmark(listener_count: int) -> None:
    database_url = os.environ.get(
        "KAPY_DATABASE_URL", "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy"
    )
    valkey_url = os.environ.get("KAPY_VALKEY_URL", "redis://127.0.0.1:56379/0")
    schema = "kapy_events_" + uuid4().hex
    channel = uuid4()
    observed: dict[UUID, list[str]] = defaultdict(list)
    cursors: dict[UUID, str | None] = {}

    async def runner(context: RunContext) -> RunResult:
        for item in context.inputs:
            payload = item.payload
            if item.event_id is not None:
                if not isinstance(payload, dict) or payload.get("event_id") != str(item.event_id):
                    raise AssertionError("Event identity was not preserved")
                payload = payload["payload"]
            if not isinstance(payload, str):
                raise AssertionError("Event payload changed shape")
            observed[context.session.id].append(payload)
        return RunResult(
            output=observed[context.session.id][-1],
            wait_for=(channel,),
            checkpoint=CheckpointWrite(
                number=context.checkpoint_number + 1,
                state=RunnerState(codec="events-acceptance-v1", data={}),
                messages=(),
                consumed_input_ids=tuple(item.id for item in context.inputs),
            ),
        )

    try:
        await migrate(database_url, schema=schema)
        async with (
            asyncio.timeout(180),
            SessionService(
                database_url=database_url,
                valkey_url=valkey_url,
                runner=runner,
                schema=schema,
                namespace=schema,
            ) as state,
        ):

            async def create_listener() -> UUID:
                created = await state.create_session(
                    SessionSpec(
                        title="Event acceptance",
                        machine_ids=(),
                        default_machine_id=None,
                        config={},
                        initial_state=RunnerState(codec="events-acceptance-v1", data={}),
                    ),
                    request_id=uuid4(),
                    input="seed",
                )
                cursors[created.session.id] = None
                return created.session.id

            async def wait_output(session_id: UUID, output: str) -> None:
                while True:
                    page = await state.read_output(
                        session_id, after=cursors[session_id], limit=200, wait_seconds=1
                    )
                    cursors[session_id] = page.next_cursor
                    for record in page.items:
                        if record.kind == "error":
                            raise AssertionError("Event runner failed")
                        if (
                            record.kind == "waiting"
                            and isinstance(record.data, dict)
                            and record.data.get("output") == output
                        ):
                            return

            early = await state.publish_event(
                channel, "backlog", request_id=uuid4(), producer_session_id=None
            )
            if early.delivered != 0 or not early.pending:
                raise AssertionError("Event published before any listener was not retained")
            first = await create_listener()
            await wait_output(first, "backlog")
            remaining = await asyncio.gather(
                *(create_listener() for _ in range(listener_count - 1))
            )
            await asyncio.gather(*(wait_output(session_id, "seed") for session_id in remaining))
            listeners = [first, *remaining]
            rounds: list[dict[str, float | int]] = []
            for round_number in range(2):
                payload = f"live-{round_number}"
                started = time.perf_counter()
                receipt = await state.publish_event(
                    channel, payload, request_id=uuid4(), producer_session_id=None
                )
                published_ms = (time.perf_counter() - started) * 1000
                if receipt.delivered != listener_count or receipt.pending:
                    raise AssertionError("Broadcast receipt did not include every subscriber")
                await asyncio.gather(
                    *(wait_output(session_id, payload) for session_id in listeners)
                )
                rounds.append(
                    {
                        "deliveries": receipt.delivered,
                        "publish_ms": round(published_ms, 2),
                        "all_listeners_waiting_ms": round(
                            (time.perf_counter() - started) * 1000, 2
                        ),
                    }
                )
            for session_id in listeners:
                expected = [
                    "seed",
                    *(["backlog"] if session_id == first else []),
                    "live-0",
                    "live-1",
                ]
                if observed[session_id] != expected:
                    raise AssertionError("Missing, duplicate or incorrectly replayed event")
            print(
                json.dumps(
                    {
                        "listeners": listener_count,
                        "backlog_delivered_to_first_listener_only": True,
                        "exact_event_sequences": True,
                        "rounds": rounds,
                    }
                )
            )
    finally:
        async with await psycopg.AsyncConnection.connect(database_url, autocommit=True) as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listeners", type=int, default=100)
    arguments = parser.parse_args()
    if arguments.listeners < 2:
        parser.error("At least two listeners are required")
    asyncio.run(benchmark(arguments.listeners))

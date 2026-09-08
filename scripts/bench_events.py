"""Measure independent one-shot handoffs, including publication before waiting."""

import argparse
import asyncio
import json
import os
import time
from uuid import uuid4

import psycopg
from psycopg import sql

from kapy.state import (
    CheckpointWrite,
    RunContext,
    RunnerState,
    RunResult,
    SessionService,
    SessionSpec,
    WaitFor,
    migrate,
)


async def benchmark(listener_count: int) -> None:
    database_url = os.environ.get(
        "KAPY_DATABASE_URL", "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy"
    )
    valkey_url = os.environ.get("KAPY_VALKEY_URL", "redis://127.0.0.1:56379/0")
    schema = "kapy_events_" + uuid4().hex
    channels = [uuid4() for _ in range(listener_count)]
    received = set()

    async def runner(context: RunContext) -> RunResult:
        channel = channels[int(context.session.title)]
        if context.inputs[0].event_id:
            item = context.inputs[0]
            assert item.event_id == channel and item.being_waited_id is None
            assert isinstance(item.payload, dict) and item.payload["output"] == "result"
            assert context.session.id not in received
            received.add(context.session.id)
            output = "done"
        else:
            output = WaitFor((channel,))
        return RunResult(
            output,
            CheckpointWrite(
                context.checkpoint_number + 1,
                context.state,
                (),
                tuple(item.id for item in context.inputs),
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
            started = time.perf_counter()
            receipts = await asyncio.gather(
                *(
                    state.publish_event(
                        channel,
                        "result",
                        request_id=uuid4(),
                        producer_session_id=None,
                    )
                    for channel in channels
                )
            )
            assert all(receipt.pending for receipt in receipts)
            sessions = await asyncio.gather(
                *(
                    state.create_session(
                        SessionSpec(str(i), (), None, {}, RunnerState("benchmark", {})),
                        request_id=uuid4(),
                        input="wait",
                    )
                    for i in range(listener_count)
                )
            )
            for session in sessions:
                assert session.submission is not None
                while True:
                    result = await state.wait_submission(
                        session.session.id, session.submission.request_id, wait_seconds=30
                    )
                    if result.completion:
                        assert result.completion.output == "done"
                        break
            assert len(received) == listener_count
            print(
                json.dumps(
                    {
                        "channels": listener_count,
                        "unique_handoffs": len(received),
                        "seconds": round(time.perf_counter() - started, 3),
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
    if arguments.listeners < 1:
        parser.error("At least one listener is required")
    asyncio.run(benchmark(arguments.listeners))

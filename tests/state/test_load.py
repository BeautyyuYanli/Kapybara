"""Architect acceptance loads; report measurements without inventing throughput targets."""

import asyncio
import json
import time
from uuid import UUID, uuid4

import pytest

from kapy.state import CheckpointWrite, RunContext, RunResult

from .conftest import Database, spec

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_hundred_sessions_twenty_inputs_and_replay(database: Database) -> None:
    seen: dict[UUID, list[int]] = {}
    active: set[UUID] = set()
    peak_active = 0

    async def runner(ctx: RunContext) -> RunResult:
        nonlocal peak_active
        assert ctx.session.id not in active
        active.add(ctx.session.id)
        peak_active = max(peak_active, len(active))
        seen.setdefault(ctx.session.id, []).extend(int(str(item.payload)) for item in ctx.inputs)
        await asyncio.sleep(0.01)
        active.remove(ctx.session.id)
        return RunResult(
            "completed",
            (),
            CheckpointWrite(
                ctx.checkpoint_number + 1, ctx.state, (), tuple(item.id for item in ctx.inputs)
            ),
        )

    service = await database.start(runner)
    sessions = await asyncio.gather(
        *(service.create_session(spec(str(i)), request_id=uuid4()) for i in range(100))
    )
    started = time.perf_counter()

    async def submit(session_id: UUID) -> None:
        for i in range(20):
            await service.submit_input(session_id, i, request_id=uuid4(), mode="queue")

    await asyncio.gather(*(submit(session.session.id) for session in sessions))
    async with asyncio.timeout(120):
        while True:
            counts = await database.rows(
                "SELECT count(*) AS completed FROM requests WHERE operation='input' "
                "AND completion IS NOT NULL"
            )
            if counts[0]["completed"] == 2000:
                break
            await asyncio.sleep(0.05)
    elapsed = time.perf_counter() - started
    assert len(seen) == 100
    assert all(values == list(range(20)) for values in seen.values())
    assert peak_active > 1
    await service.__aexit__(None, None, None)
    reopened = await database.start(runner)
    replayed = 0
    for session in sessions:
        page = await reopened.read_output(session.session.id, limit=7)
        records = list(page.items)
        while page.has_more:
            page = await reopened.read_output(session.session.id, after=page.next_cursor, limit=7)
            records.extend(page.items)
        values = [record.data for record in records if record.kind == "input"]
        assert values == list(range(20))
        replayed += len(values)
    assert replayed == 2000
    print(
        "STATE_LOAD "
        + json.dumps(
            {
                "sessions": 100,
                "accepted": 2000,
                "completed": 2000,
                "replayed": replayed,
                "ordered_sessions": len(seen),
                "seconds": round(elapsed, 3),
                "inputs_per_second": round(2000 / elapsed, 2),
                "peak_concurrent_runners": peak_active,
            }
        )
    )


async def test_broadcast_to_hundred_listeners(database: Database) -> None:
    channel = uuid4()
    event_sessions: set[UUID] = set()
    all_received = asyncio.Event()

    async def runner(ctx: RunContext) -> RunResult:
        if any(isinstance(item.payload, dict) for item in ctx.inputs):
            assert ctx.session.id not in event_sessions
            event_sessions.add(ctx.session.id)
            if len(event_sessions) == 100:
                all_received.set()
        return RunResult(
            "completed",
            (channel,),
            CheckpointWrite(
                ctx.checkpoint_number + 1, ctx.state, (), tuple(item.id for item in ctx.inputs)
            ),
        )

    service = await database.start(runner)
    await asyncio.gather(
        *(
            service.create_session(spec(str(i)), request_id=uuid4(), input="subscribe")
            for i in range(100)
        )
    )
    async with asyncio.timeout(60):
        while True:
            subscribed = await database.rows(
                "SELECT count(*) AS count FROM subscriptions WHERE channel_id=%s", (channel,)
            )
            if subscribed[0]["count"] == 100:
                break
            await asyncio.sleep(0.02)
    started = time.perf_counter()
    receipt = await service.publish_event(
        channel, "broadcast", request_id=uuid4(), producer_session_id=None
    )
    assert receipt.delivered == 100 and not receipt.pending
    await asyncio.wait_for(all_received.wait(), 30)
    delivery_seconds = time.perf_counter() - started
    async with asyncio.timeout(30):
        while True:
            rows = await database.rows(
                "SELECT count(*) AS count FROM inputs WHERE event_id=%s AND state='consumed'",
                (receipt.event_id,),
            )
            if rows[0]["count"] == 100:
                break
            await asyncio.sleep(0.02)
    completion_seconds = time.perf_counter() - started
    assert len(event_sessions) == 100
    print(
        "EVENT_LOAD "
        + json.dumps(
            {
                "listeners": 100,
                "distinct_delivered": 100,
                "consumed": 100,
                "delivery_seconds": round(delivery_seconds, 3),
                "completion_seconds": round(completion_seconds, 3),
            }
        )
    )

"""Architect acceptance loads; report measurements without inventing throughput targets."""

import asyncio
import json
import time
from uuid import UUID, uuid4

import pytest

from kapy.state import CheckpointWrite, RunContext, RunResult, WaitFor

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
                "SELECT count(*) FILTER (WHERE completion->>'outcome'='completed') AS completed, "
                "count(*) FILTER (WHERE completion->>'outcome'='failed') AS failed "
                "FROM requests WHERE operation='input'"
            )
            assert counts[0]["failed"] == 0, "load inputs must complete successfully"
            completed = counts[0]["completed"]
            if completed == 2000:
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
                "completed": completed,
                "replayed": replayed,
                "ordered_sessions": len(seen),
                "seconds": round(elapsed, 3),
                "inputs_per_second": round(2000 / elapsed, 2),
                "peak_concurrent_runners": peak_active,
            }
        )
    )


async def test_hundred_independent_one_shot_handoffs(database: Database) -> None:
    channels = [uuid4() for _ in range(100)]
    received = set()

    async def runner(ctx: RunContext) -> RunResult:
        if ctx.inputs[0].event_id:
            received.add(ctx.session.id)
            output = "done"
        else:
            output = WaitFor((channels[int(ctx.session.title)],))
        return RunResult(
            output,
            CheckpointWrite(
                ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in ctx.inputs)
            ),
        )

    service = await database.start(runner)
    await asyncio.gather(
        *(
            service.publish_event(channel, "ready", request_id=uuid4(), producer_session_id=None)
            for channel in channels
        )
    )
    sessions = await asyncio.gather(
        *(
            service.create_session(spec(str(i)), request_id=uuid4(), input="wait")
            for i in range(100)
        )
    )
    async with asyncio.timeout(60):
        while len(received) != 100:  # noqa: ASYNC110 - bounded DB observation
            await asyncio.sleep(0.02)
    assert len(await database.rows("SELECT id FROM inputs WHERE event_id IS NOT NULL")) == 100
    assert len({s.session.id for s in sessions}) == 100

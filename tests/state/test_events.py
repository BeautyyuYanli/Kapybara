import asyncio
from uuid import UUID, uuid4

import pytest

from kapy.state import CheckpointWrite, RunContext, RunResult

from .conftest import Database, spec
from .test_service import result

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def wait_deliveries(seen: dict[UUID, list[object]], session_id: UUID, count: int) -> None:
    async with asyncio.timeout(10):
        while len(seen.get(session_id, [])) < count:  # noqa: ASYNC110 - bounded observation
            await asyncio.sleep(0.01)


async def test_sticky_broadcast_backlog_self_exclusion_and_restart(database: Database) -> None:
    channel = uuid4()
    seen: dict[UUID, list[object]] = {}

    async def runner(ctx: RunContext) -> RunResult:
        for item in ctx.inputs:
            if isinstance(item.payload, dict) and item.payload.get("type") == "event":
                seen.setdefault(ctx.session.id, []).append(item.payload["payload"])
        return result(ctx, waits=(channel,))

    service = await database.start(runner)
    early = await service.publish_event(
        channel, "early", request_id=uuid4(), producer_session_id=None
    )
    assert early.pending and early.delivered == 0
    first = await service.create_session(spec("a"), request_id=uuid4(), input="subscribe")
    await database.completed(first.submission.request_id)
    await wait_deliveries(seen, first.session.id, 1)
    assert seen[first.session.id] == ["early"]
    second = await service.create_session(spec("b"), request_id=uuid4(), input="subscribe")
    await database.completed(second.submission.request_id)
    assert second.session.id not in seen
    key = uuid4()
    broadcast = await service.publish_event(
        channel, "broadcast", request_id=key, producer_session_id=None
    )
    assert broadcast.delivered == 2
    assert (
        await service.publish_event(channel, "broadcast", request_id=key, producer_session_id=None)
        == broadcast
    )
    await wait_deliveries(seen, first.session.id, 2)
    await wait_deliveries(seen, second.session.id, 1)
    own = await service.publish_event(
        channel, "self excluded", request_id=uuid4(), producer_session_id=first.session.id
    )
    assert own.delivered == 1
    await wait_deliveries(seen, second.session.id, 2)
    assert seen[first.session.id] == ["early", "broadcast"]
    await service.__aexit__(None, None, None)
    reopened = await database.start(runner)
    after = await reopened.publish_event(
        channel, "after restart", request_id=uuid4(), producer_session_id=None
    )
    assert after.delivered == 2
    await wait_deliveries(seen, first.session.id, 3)
    await wait_deliveries(seen, second.session.id, 3)
    default_events = await database.rows(
        "SELECT i.id FROM inputs i JOIN events e ON e.id=i.event_id "
        "WHERE i.session_id=e.producer_session_id"
    )
    assert default_events == []


async def test_recursive_completion_before_parent_subscribes_and_repeated_wait(
    database: Database,
) -> None:
    waiting_id = uuid4()
    seen: dict[UUID, list[object]] = {}

    async def runner(ctx: RunContext) -> RunResult:
        if ctx.session.title == "parent":
            for item in ctx.inputs:
                if isinstance(item.payload, dict):
                    seen.setdefault(ctx.session.id, []).append(item.payload["payload"])
            return result(ctx, waits=(waiting_id,))
        return result(ctx, "child answer")

    service = await database.start(runner)
    child = await service.create_session(
        spec("child"), request_id=uuid4(), input="do it", waiting_id=waiting_id
    )
    await database.completed(child.submission.request_id)
    parent = await service.create_session(spec("parent"), request_id=uuid4(), input="wait now")
    await database.completed(parent.submission.request_id)
    await wait_deliveries(seen, parent.session.id, 1)
    completion = seen[parent.session.id][0]
    assert isinstance(completion, dict)
    assert completion["request_ids"] == [str(child.submission.request_id)]
    assert completion["output"] == "child answer"
    another = await service.submit_input(
        child.session.id, "again", request_id=uuid4(), waiting_id=waiting_id
    )
    await database.completed(another.request_id)
    await wait_deliveries(seen, parent.session.id, 2)
    second = seen[parent.session.id][1]
    assert isinstance(second, dict)
    assert second["request_ids"] == [str(another.request_id)]


async def test_unsubscribe_retains_already_delivered_queue(database: Database) -> None:
    channel = uuid4()
    started, release = asyncio.Event(), asyncio.Event()
    seen: list[str] = []

    async def runner(ctx: RunContext) -> RunResult:
        payload = ctx.inputs[0].payload
        if payload == "subscribe":
            return result(ctx, waits=(channel,))
        if payload == "work":
            started.set()
            await release.wait()
        for item in ctx.inputs:
            if isinstance(item.payload, dict):
                seen.append(str(item.payload["payload"]))
        return result(ctx)  # removes external channel only at successful waiting

    service = await database.start(runner)
    created = await service.create_session(spec(), request_id=uuid4(), input="subscribe")
    await database.completed(created.submission.request_id)
    work = await service.submit_input(created.session.id, "work", request_id=uuid4())
    await asyncio.wait_for(started.wait(), 5)
    receipt = await service.publish_event(
        channel,
        "queued before unsubscribe",
        request_id=uuid4(),
        producer_session_id=None,
        mode="queue",
    )
    assert receipt.delivered == 1
    release.set()
    await database.completed(work.request_id)
    async with asyncio.timeout(5):
        while not seen:  # noqa: ASYNC110 - bounded observation
            await asyncio.sleep(0.01)
    assert seen == ["queued before unsubscribe"]
    assert (
        await service.publish_event(
            channel, "after unsubscribe", request_id=uuid4(), producer_session_id=None
        )
    ).pending


async def test_crash_before_checkpoint_redelivers_reserved_event(database: Database) -> None:
    channel = uuid4()
    ready = asyncio.Event()
    attempts: list[tuple[int, object]] = []

    async def initial(ctx: RunContext) -> RunResult:
        if ctx.inputs[0].payload == "subscribe":
            return result(ctx, waits=(channel,))
        attempts.append((ctx.attempt, ctx.inputs[0].payload))
        ready.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    service = await database.start(initial)
    created = await service.create_session(spec(), request_id=uuid4(), input="subscribe")
    await database.completed(created.submission.request_id)
    receipt = await service.publish_event(
        channel, "durable event", request_id=uuid4(), producer_session_id=None
    )
    await asyncio.wait_for(ready.wait(), 5)
    await service.__aexit__(None, None, None)
    finished = asyncio.Event()

    async def resume(ctx: RunContext) -> RunResult:
        attempts.append((ctx.attempt, ctx.inputs[0].payload))
        assert ctx.recovered
        finished.set()
        return RunResult(
            "done",
            (channel,),
            CheckpointWrite(
                ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in ctx.inputs)
            ),
        )

    await database.start(resume)
    await asyncio.wait_for(finished.wait(), 5)
    assert attempts[0][0] == 1 and attempts[1][0] == 2
    assert attempts[0][1] == attempts[1][1]
    assert (
        len(await database.rows("SELECT id FROM inputs WHERE event_id=%s", (receipt.event_id,)))
        == 1
    )


async def test_only_producer_listener_keeps_backlog_for_later_listener(database: Database) -> None:
    own_channel: list[UUID] = []
    delivered = asyncio.Event()
    seen: list[object] = []

    async def runner(ctx: RunContext) -> RunResult:
        for item in ctx.inputs:
            if isinstance(item.payload, dict) and item.payload.get("payload") == "self-only":
                seen.append(item.payload)
                delivered.set()
        return result(ctx, waits=tuple(own_channel))

    service = await database.start(runner)
    producer = await service.create_session(spec("producer"), request_id=uuid4())
    channel = producer.session.id
    own_channel.append(channel)
    receipt = await service.publish_event(
        channel, "self-only", request_id=uuid4(), producer_session_id=producer.session.id
    )
    assert receipt.pending
    other = await service.create_session(spec("other"), request_id=uuid4(), input="subscribe")
    await database.completed(other.submission.request_id)
    await asyncio.wait_for(delivered.wait(), 5)
    assert len(seen) == 1
    rows = await database.rows(
        "SELECT session_id FROM inputs WHERE event_id=%s", (receipt.event_id,)
    )
    assert [row["session_id"] for row in rows] == [other.session.id]

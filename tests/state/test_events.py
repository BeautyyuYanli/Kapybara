"""One-shot handoff, reply obligations and durable waiting transitions."""

import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from kapy.state import CheckpointWrite, Conflict, ReplyTo, RunContext, RunResult

from .conftest import Database, spec
from .test_service import result

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def waiting(database: Database, session_id: UUID) -> None:
    async with asyncio.timeout(10):
        while True:
            rows = await database.rows(
                "SELECT 1 FROM sessions s WHERE id=%s AND status='waiting' "
                "AND EXISTS(SELECT 1 FROM runs r "
                "WHERE r.id=s.latest_run_id AND r.status='waiting')",
                (session_id,),
            )
            if rows:
                return
            await asyncio.sleep(0.01)


async def test_publish_before_wait_delivers_once_across_restart(database: Database) -> None:
    channel, publish_id = uuid4(), uuid4()
    seen = []

    async def runner(ctx: RunContext) -> RunResult:
        if ctx.inputs[0].payload == "wait":
            return result(ctx, waits=(channel,))
        seen.extend(ctx.inputs)
        return result(ctx, "done")

    service = await database.start(runner)
    receipt = await service.publish_event(
        channel, "ready", request_id=publish_id, producer_session_id=None
    )
    assert receipt.pending and receipt.event_id == channel
    await service.__aexit__(None, None, None)
    service = await database.start(runner)
    parent = await service.create_session(spec(), request_id=uuid4(), input="wait")
    assert parent.submission is not None
    completed = await service.wait_submission(
        parent.session.id, parent.submission.request_id, wait_seconds=10
    )
    assert completed.completion and completed.completion.output == "done"
    assert len(seen) == 1 and seen[0].event_id == channel
    assert seen[0].being_waited_id is None
    assert seen[0].payload["output"] == "ready"
    assert (
        await service.publish_event(
            channel, "ready", request_id=publish_id, producer_session_id=None
        )
        == receipt
    )
    with pytest.raises(Conflict):
        await service.publish_event(channel, "second", request_id=uuid4(), producer_session_id=None)
    assert (await database.rows("SELECT state FROM waiting_channels WHERE id=%s", (channel,)))[0][
        "state"
    ] == "delivered"


async def test_wait_does_not_reply_and_direct_input_gets_fresh_address(database: Database) -> None:
    channel = uuid4()
    addresses = []

    async def runner(ctx: RunContext) -> RunResult:
        addresses.extend(i.being_waited_id for i in ctx.inputs)
        if ctx.inputs[0].payload == "wait":
            return result(ctx, waits=(channel,))
        return result(ctx, "answer all read inputs")

    service = await database.start(runner)
    created = await service.create_session(spec(), request_id=uuid4(), input="wait")
    assert created.submission
    await waiting(database, created.session.id)
    assert (
        await service.wait_submission(created.session.id, created.submission.request_id)
    ).completion is None
    key = uuid4()
    followup = await service.submit_input(
        created.session.id, "continue", request_id=key, mode="steer"
    )
    assert followup.waiting_id != created.submission.waiting_id
    assert (
        await service.submit_input(created.session.id, "continue", request_id=key, mode="steer")
        == followup
    )
    await database.completed(followup.request_id)
    first = await service.wait_submission(created.session.id, created.submission.request_id)
    assert first.completion and first.completion.output == "answer all read inputs"
    assert addresses == [created.submission.waiting_id, followup.waiting_id]
    assert not await database.rows(
        "SELECT 1 FROM waiting_channels WHERE id=%s", (created.session.id,)
    )


async def test_reply_to_selects_old_input_and_preserves_full_output(database: Database) -> None:
    channel = uuid4()
    first_address = None

    async def runner(ctx: RunContext) -> RunResult:
        nonlocal first_address
        if ctx.inputs[0].payload == "first":
            first_address = ctx.inputs[0].being_waited_id
            return result(ctx, waits=(channel,))
        await ctx.checkpoint(
            CheckpointWrite(
                ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in ctx.inputs)
            )
        )
        page = await ctx.unreplied_addresses()
        assert first_address is not None
        assert (first_address in page.being_waited_ids) == (ctx.inputs[0].payload == "second")
        targets = (first_address,) if ctx.inputs[0].payload == "second" else page.being_waited_ids
        return RunResult(
            ReplyTo(targets, "complete body"),
            CheckpointWrite(ctx.checkpoint_number + 1, ctx.state, (), ()),
        )

    service = await database.start(runner)
    created = await service.create_session(
        replace(spec(), config={"output_mode": "reply_to"}), request_id=uuid4(), input="first"
    )
    assert created.submission
    await waiting(database, created.session.id)
    second = await service.submit_input(created.session.id, "second", request_id=uuid4())
    first = await service.wait_submission(
        created.session.id, created.submission.request_id, wait_seconds=10
    )
    assert first.completion and first.completion.output == ReplyTo(
        (created.submission.waiting_id,), "complete body"
    )
    assert (await service.wait_submission(created.session.id, second.request_id)).completion is None
    # No implicit rerun is created to process an unselected input.
    assert len(await database.rows("SELECT id FROM runs")) == 2
    third = await service.submit_input(created.session.id, "third", request_id=uuid4())
    await database.completed(third.request_id)
    second_result = await service.wait_submission(created.session.id, second.request_id)
    assert second_result.completion and isinstance(second_result.completion.output, ReplyTo)
    assert second_result.completion.output.being_waited_ids == (second.waiting_id, third.waiting_id)
    row = (
        await database.rows(
            "SELECT output FROM waiting_channels WHERE request_id=%s", (second.request_id,)
        )
    )[0]
    assert row["output"] == {
        "kind": "reply_to",
        "being_waited_ids": [str(second.waiting_id), str(third.waiting_id)],
        "payload": "complete body",
    }


@pytest.mark.parametrize("invalid_target", ["foreign", "queued", "settled"])
async def test_mixed_reply_targets_reject_the_whole_completion(
    database: Database, invalid_target: str
) -> None:
    started, release, delivered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    valid_address = invalid_address = None
    received = []

    async def runner(ctx: RunContext) -> RunResult:
        if ctx.session.title == "receiver":
            if ctx.inputs[0].event_id is None:
                return result(ctx, waits=(valid_address,))
            received.extend(item.payload for item in ctx.inputs)
            delivered.set()
            return result(ctx)
        if ctx.inputs[0].payload == "seed":
            targets = (ctx.inputs[0].being_waited_id,)
        elif ctx.inputs[0].payload == "reply":
            started.set()
            await release.wait()
            targets = (valid_address, invalid_address)
        else:
            await asyncio.Event().wait()
            raise AssertionError("unrelated input must remain unfinished")
        return RunResult(
            ReplyTo(targets, "successful reply"),
            CheckpointWrite(
                ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in ctx.inputs)
            ),
        )

    service = await database.start(runner)
    receiver = await service.create_session(spec("receiver"), request_id=uuid4())
    producer = await service.create_session(
        replace(spec("producer"), config={"output_mode": "reply_to"}),
        request_id=uuid4(),
        input="seed" if invalid_target == "settled" else None,
    )
    if producer.submission is not None:
        await database.completed(producer.submission.request_id)
    valid = await service.submit_input(
        producer.session.id,
        "reply",
        request_id=uuid4(),
        receiver_session_id=receiver.session.id,
    )
    valid_address = valid.waiting_id
    await asyncio.wait_for(started.wait(), 5)
    if invalid_target == "foreign":
        other = await service.create_session(spec("other"), request_id=uuid4(), input="foreign")
        assert other.submission is not None
        invalid = other.submission
    elif invalid_target == "queued":
        invalid = await service.submit_input(
            producer.session.id, "queued", request_id=uuid4(), mode="queue"
        )
        assert (
            await database.rows("SELECT state FROM inputs WHERE id=%s", (invalid.input_id,))
        ) == [{"state": "pending"}]
    else:
        assert producer.submission is not None
        invalid = producer.submission
    invalid_address = invalid.waiting_id
    before = await database.rows("SELECT * FROM waiting_channels WHERE id=%s", (invalid_address,))
    before_status = await service.wait_submission(invalid.session_id, invalid.request_id)
    await service.submit_input(receiver.session.id, "listen", request_id=uuid4())
    await waiting(database, receiver.session.id)

    release.set()
    failed = await service.wait_submission(producer.session.id, valid.request_id, wait_seconds=10)
    assert failed.completion and failed.completion.outcome == "failed"
    assert failed.completion.output is None
    await asyncio.wait_for(delivered.wait(), 5)
    assert len(received) == 1
    assert received[0]["outcome"] == "failed" and received[0]["output"] is None
    assert (
        await database.rows("SELECT * FROM waiting_channels WHERE id=%s", (invalid_address,))
        == before
    )
    assert await service.wait_submission(invalid.session_id, invalid.request_id) == before_status
    # The final checkpoint and final output share the rejected completion transaction.
    assert (
        await database.rows(
            "SELECT 1 FROM checkpoints WHERE run_id=%s", (failed.completion.run_id,)
        )
        == []
    )
    assert (
        await database.rows(
            "SELECT 1 FROM records WHERE run_id=%s AND kind='final'", (failed.completion.run_id,)
        )
        == []
    )


async def test_failure_settles_old_and_current_inputs_but_leaves_queue_for_later(
    database: Database,
) -> None:
    channel = uuid4()
    second_started, fail_second = asyncio.Event(), asyncio.Event()
    third_started, finish_third = asyncio.Event(), asyncio.Event()
    runs = {}

    async def runner(ctx: RunContext) -> RunResult:
        payload = ctx.inputs[0].payload
        runs[payload] = ctx.run_id
        if payload == "A":
            return result(ctx, waits=(channel,))
        if payload == "B":
            await ctx.checkpoint(
                CheckpointWrite(
                    ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in ctx.inputs)
                )
            )
            second_started.set()
            await fail_second.wait()
            raise RuntimeError("second run failed")
        assert payload == "C"
        third_started.set()
        await finish_third.wait()
        return result(ctx, "later queue completed")

    service = await database.start(runner)
    first = await service.create_session(spec(), request_id=uuid4(), input="A")
    assert first.submission is not None
    await waiting(database, first.session.id)
    second = await service.submit_input(first.session.id, "B", request_id=uuid4())
    await asyncio.wait_for(second_started.wait(), 5)
    third = await service.submit_input(first.session.id, "C", request_id=uuid4(), mode="queue")
    assert (await database.rows("SELECT state FROM inputs WHERE id=%s", (third.input_id,))) == [
        {"state": "pending"}
    ]
    fail_second.set()
    for receipt in (first.submission, second):
        completed = await service.wait_submission(
            first.session.id, receipt.request_id, wait_seconds=10
        )
        assert completed.completion and completed.completion.outcome == "failed"
        assert completed.completion.run_id == runs["B"] != runs["A"]
        assert completed.completion.output is None
    await asyncio.wait_for(third_started.wait(), 5)
    assert (await service.wait_submission(first.session.id, third.request_id)).completion is None
    assert (
        await database.rows("SELECT state FROM waiting_channels WHERE id=%s", (third.waiting_id,))
    ) == [{"state": "open"}]
    assert runs["C"] != runs["B"]
    finish_third.set()
    completed = await service.wait_submission(first.session.id, third.request_id, wait_seconds=10)
    assert completed.completion and completed.completion.outcome == "completed"
    assert completed.completion.output == "later queue completed"


async def test_ready_channels_wake_once_and_remaining_wait_survives_the_wake(
    database: Database,
) -> None:
    first, second, third = uuid4(), uuid4(), uuid4()
    woke, release = asyncio.Event(), asyncio.Event()
    received = []

    async def runner(ctx: RunContext) -> RunResult:
        if ctx.inputs[0].event_id is None:
            return result(ctx, waits=(first, second, third))
        received.extend(ctx.inputs)
        woke.set()
        await release.wait()
        late = await ctx.poll_steer()
        received.extend(late)
        return RunResult(
            "received all three",
            CheckpointWrite(
                ctx.checkpoint_number + 1,
                ctx.state,
                (),
                tuple(i.id for i in (*ctx.inputs, *late)),
            ),
        )

    service = await database.start(runner)
    for channel in (first, second):
        receipt = await service.publish_event(
            channel, str(channel), request_id=uuid4(), producer_session_id=None
        )
        assert receipt.pending
    created = await service.create_session(spec(), request_id=uuid4(), input="wait for all")
    assert created.submission is not None
    await asyncio.wait_for(woke.wait(), 5)
    assert {i.event_id for i in received} == {first, second}
    assert (
        await database.rows("SELECT state,active FROM waiting_channels WHERE id=%s", (third,))
    ) == [{"state": "open", "active": True}]
    receipt = await service.publish_event(
        third, "third result", request_id=uuid4(), producer_session_id=None
    )
    assert receipt.delivered == 1
    release.set()
    completed = await service.wait_submission(
        created.session.id, created.submission.request_id, wait_seconds=10
    )
    assert completed.completion and completed.completion.output == "received all three"
    assert len(received) == 3 and {i.event_id for i in received} == {first, second, third}
    assert all(i.being_waited_id is None for i in received)
    assert await database.rows(
        "SELECT event_id,state FROM inputs WHERE event_id=ANY(%s) ORDER BY event_id",
        ([first, second, third],),
    ) == [{"event_id": channel, "state": "consumed"} for channel in sorted((first, second, third))]


async def test_single_receiver_binding_survives_wait_replacement(database: Database) -> None:
    channel, another = uuid4(), uuid4()

    async def runner(ctx: RunContext) -> RunResult:
        return result(ctx, waits=(channel if ctx.inputs[0].payload == "first" else another,))

    service = await database.start(runner)
    first = await service.create_session(spec(), request_id=uuid4(), input="first")
    assert first.submission
    await waiting(database, first.session.id)
    new = await service.submit_input(first.session.id, "replace", request_id=uuid4())
    async with asyncio.timeout(10):
        while not await database.rows(  # noqa: ASYNC110 - bounded DB observation
            "SELECT 1 FROM waiting_channels WHERE id=%s AND active", (another,)
        ):
            await asyncio.sleep(0.01)
    other = await service.create_session(spec(), request_id=uuid4(), input="first")
    assert other.submission
    failed = await service.wait_submission(
        other.session.id, other.submission.request_id, wait_seconds=10
    )
    assert failed.completion and failed.completion.outcome == "failed"
    rows = await database.rows(
        "SELECT receiver_session_id,active FROM waiting_channels WHERE id=%s", (channel,)
    )
    assert rows[0] == {"receiver_session_id": first.session.id, "active": False}
    assert (await service.wait_submission(first.session.id, new.request_id)).completion is None


async def test_self_producer_is_not_special_and_queue_survives_clear(database: Database) -> None:
    channel = uuid4()
    started, release, received = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def runner(ctx: RunContext) -> RunResult:
        if ctx.inputs[0].payload == "wait":
            return result(ctx, waits=(channel,))
        if ctx.inputs[0].payload == "work":
            started.set()
            await release.wait()
        if ctx.inputs[0].event_id:
            received.set()
        return result(ctx)

    service = await database.start(runner)
    created = await service.create_session(spec(), request_id=uuid4(), input="wait")
    assert created.submission
    await waiting(database, created.session.id)
    work = await service.submit_input(created.session.id, "work", request_id=uuid4())
    await asyncio.wait_for(started.wait(), 5)
    receipt = await service.publish_event(
        channel,
        "self result",
        request_id=uuid4(),
        producer_session_id=created.session.id,
        mode="queue",
    )
    assert receipt.delivered == 1
    release.set()
    await database.completed(work.request_id)
    await asyncio.wait_for(received.wait(), 5)
    assert len(await database.rows("SELECT id FROM inputs WHERE event_id=%s", (channel,))) == 1


async def test_reply_channel_cannot_be_published_and_empty_create_has_no_submission(
    database: Database,
) -> None:
    pause = asyncio.Event()

    async def runner(ctx: RunContext) -> RunResult:
        await pause.wait()
        return result(ctx)

    service = await database.start(runner)
    created = await service.create_session(spec(), request_id=uuid4())
    assert created.submission is None
    assert await database.rows("SELECT * FROM waiting_channels") == []
    one = await service.submit_input(created.session.id, "one", request_id=uuid4(), mode="queue")
    two = await service.submit_input(created.session.id, "two", request_id=uuid4(), mode="steer")
    assert one.waiting_id != two.waiting_id
    with pytest.raises(Conflict):
        await service.publish_event(
            one.waiting_id, "spoof", request_id=uuid4(), producer_session_id=created.session.id
        )


async def test_interrupted_delivery_resumes_same_input(database: Database) -> None:
    channel = uuid4()
    ready = asyncio.Event()
    seen = []

    async def initial(ctx: RunContext) -> RunResult:
        if not ctx.inputs[0].event_id:
            return result(ctx, waits=(channel,))
        seen.append(ctx.inputs[0])
        ready.set()
        await asyncio.Event().wait()
        raise AssertionError

    service = await database.start(initial)
    created = await service.create_session(spec(), request_id=uuid4(), input="wait")
    assert created.submission
    await waiting(database, created.session.id)
    await service.publish_event(channel, "durable", request_id=uuid4(), producer_session_id=None)
    await asyncio.wait_for(ready.wait(), 5)
    await service.__aexit__(None, None, None)

    async def resume(ctx: RunContext) -> RunResult:
        assert ctx.recovered and ctx.attempt == 2
        seen.append(ctx.inputs[0])
        return result(ctx)

    service = await database.start(resume)
    await database.completed(created.submission.request_id)
    assert seen[0] == seen[1]
    assert len(await database.rows("SELECT id FROM inputs WHERE event_id=%s", (channel,))) == 1


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("size", [100_000, 50_000])
async def test_completion_backlog_is_validated_before_producer_commits(database, explicit, size):
    body = '"' * size
    address = None
    received = []

    async def runner(ctx):
        if ctx.session.title == "producer":
            output = ReplyTo((ctx.inputs[0].being_waited_id,), body) if explicit else body
            return RunResult(
                output,
                CheckpointWrite(
                    ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in ctx.inputs)
                ),
            )
        if ctx.inputs[0].event_id is None:
            return result(ctx, waits=(address,))
        received.append(ctx.inputs[0].payload)
        return result(ctx, "observed")

    service = await database.start(runner)
    producer = await service.create_session(
        replace(spec("producer"), config={"output_mode": "reply_to" if explicit else "text"}),
        request_id=uuid4(),
        input="produce",
    )
    assert producer.submission is not None
    address = producer.submission.waiting_id
    produced = await service.wait_submission(
        producer.session.id, producer.submission.request_id, wait_seconds=10
    )
    assert produced.completion is not None
    expected_outcome = "failed" if size == 100_000 else "completed"
    expected_output = (
        None
        if size == 100_000
        else (
            {"kind": "reply_to", "being_waited_ids": [str(address)], "payload": body}
            if explicit
            else body
        )
    )
    assert produced.completion.outcome == expected_outcome
    channel = (
        await database.rows(
            "SELECT state,output,outcome FROM waiting_channels WHERE id=%s", (address,)
        )
    )[0]
    assert channel == {"state": "ready", "output": expected_output, "outcome": expected_outcome}
    # Subscribe only after publication: an accepted backlog must not poison the receiver.
    receiver = await service.create_session(spec("receiver"), request_id=uuid4(), input="wait")
    assert receiver.submission is not None
    observed = await service.wait_submission(
        receiver.session.id, receiver.submission.request_id, wait_seconds=10
    )
    assert observed.completion is not None and observed.completion.outcome == "completed"
    assert received[0]["output"] == expected_output
    assert received[0]["outcome"] == expected_outcome
    assert (await database.rows("SELECT state FROM waiting_channels WHERE id=%s", (address,)))[0][
        "state"
    ] == "delivered"

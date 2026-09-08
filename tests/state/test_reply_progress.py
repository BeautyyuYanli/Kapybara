"""Live reply transactions remain durable independently of the enclosing run."""

import asyncio
import json
from dataclasses import replace
from uuid import uuid4

import httpx2
import pytest
from agent.test_runner import response
from agent.test_runner import runner as ai_runner

from kapy.state import CheckpointWrite, Conflict, InvalidArgument, ReplyTo, RunResult, WaitFor

from .conftest import Database, spec
from .test_events import waiting

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.mark.parametrize("outcome", ["completed", "failed", "deleted", "restart"])
async def test_partial_reply_is_observable_and_survives_later_outcome(database: Database, outcome):
    start, accept, replied, finish = (asyncio.Event() for _ in range(4))
    emission = uuid4()
    saved = None
    first_output = None
    receiver_inputs = []
    received = asyncio.Event()

    async def runner(ctx):
        nonlocal saved, first_output
        if ctx.session.title == "receiver":
            receiver_inputs.extend(ctx.inputs)
            received.set()
            return RunResult(
                "received",
                CheckpointWrite(
                    ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in ctx.inputs)
                ),
            )
        if not ctx.recovered:
            start.set()
            await accept.wait()
            inputs = (*ctx.inputs, *await ctx.poll_steer())
            await ctx.checkpoint(
                CheckpointWrite(
                    ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in inputs)
                )
            )
            first_output = ReplyTo((inputs[0].being_waited_id,), "first reply")
        assert first_output is not None
        checked = []
        saved = await ctx.reply(
            emission_id=emission, output=first_output, validate_receipt=checked.append
        )
        assert (
            await ctx.reply(
                emission_id=emission, output=first_output, validate_receipt=checked.append
            )
            == saved
        )
        assert checked == [saved, saved]
        with pytest.raises(Conflict):
            await ctx.reply(emission_id=emission, output=replace(first_output, payload="different"))
        with pytest.raises(InvalidArgument):
            await ctx.reply(emission_id=uuid4(), output=first_output)
        assert len(saved.remaining_being_waited_ids) == 1
        with pytest.raises(InvalidArgument):
            await ctx.reply(
                emission_id=uuid4(),
                output=ReplyTo(
                    (*saved.remaining_being_waited_ids, uuid4()), "atomic invalid selection"
                ),
            )
        assert (
            await ctx.unreplied_addresses()
        ).being_waited_ids == saved.remaining_being_waited_ids
        replied.set()
        await finish.wait()
        if outcome == "failed":
            raise RuntimeError("later model failure")
        last = await ctx.reply(
            emission_id=uuid4(), output=ReplyTo(saved.remaining_being_waited_ids, "second reply")
        )
        assert not last.remaining_being_waited_ids
        return RunResult(last.output, CheckpointWrite(ctx.checkpoint_number + 1, ctx.state, (), ()))

    service = await database.start(runner)
    receiver = await service.create_session(spec("receiver"), request_id=uuid4())
    created = await service.create_session(
        replace(spec("producer"), config={"output_mode": "reply_to"}),
        request_id=uuid4(),
        input="first",
        receiver_session_id=receiver.session.id,
    )
    first = created.submission
    assert first is not None
    await asyncio.wait_for(start.wait(), 5)
    second = await service.submit_input(
        created.session.id, "second", request_id=uuid4(), mode="steer"
    )
    # The receiver can consume a reply before the producer's run finishes.
    async with service._store.write() as conn:
        await service._wait_for(conn, receiver.session.id, (first.waiting_id,))
    accept.set()
    await asyncio.wait_for(replied.wait(), 5)
    first_status = await service.wait_submission(created.session.id, first.request_id)
    assert first_status.completion is not None
    assert first_status.completion.output == first_output
    assert (await service.get_session(created.session.id)).status == "running"
    assert (await service.wait_submission(created.session.id, second.request_id)).completion is None
    await asyncio.wait_for(received.wait(), 5)
    assert receiver_inputs[0].payload["output"]["payload"] == "first reply"
    assert (
        len(
            await database.rows(
                "SELECT 1 FROM records WHERE session_id=%s AND kind='reply'", (created.session.id,)
            )
        )
        == 1
    )
    if outcome == "deleted":
        await service.delete_session(created.session.id, request_id=uuid4())
    else:
        if outcome == "restart":
            await service.__aexit__(None, None, None)
            replied.clear()
            service = await database.start(runner)
            await asyncio.wait_for(replied.wait(), 5)
        finish.set()
    await database.completed(second.request_id)
    assert await service.wait_submission(created.session.id, first.request_id) == first_status
    second_status = await service.wait_submission(created.session.id, second.request_id)
    assert second_status.completion is not None
    assert second_status.completion.outcome == (
        outcome if outcome in {"failed", "deleted"} else "completed"
    )
    if outcome != "deleted":
        rows = await database.rows(
            "SELECT state FROM inputs WHERE event_id=%s", (first.waiting_id,)
        )
        assert len(rows) == 1


@pytest.mark.parametrize("invalid_final", ["uncommitted", "partial", "older"])
async def test_invalid_final_reply_cannot_finish_run(database: Database, invalid_final):
    started, ready = asyncio.Event(), asyncio.Event()
    run_id = None

    async def runner(ctx):
        nonlocal run_id
        run_id = ctx.run_id
        started.set()
        await ready.wait()
        inputs = (*ctx.inputs, *await ctx.poll_steer())
        await ctx.checkpoint(
            CheckpointWrite(ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in inputs))
        )
        first_reply = ReplyTo((inputs[0].being_waited_id,), "first answer")
        if invalid_final != "uncommitted":
            receipt = await ctx.reply(emission_id=uuid4(), output=first_reply)
            assert receipt.remaining_being_waited_ids == (inputs[1].being_waited_id,)
            if invalid_final == "older":
                receipt = await ctx.reply(
                    emission_id=uuid4(),
                    output=ReplyTo(receipt.remaining_being_waited_ids, "second answer"),
                )
                assert receipt.remaining_being_waited_ids == ()
        return RunResult(
            first_reply,
            CheckpointWrite(ctx.checkpoint_number + 1, ctx.state, (), ()),
        )

    service = await database.start(runner)
    created = await service.create_session(
        replace(spec(), config={"output_mode": "reply_to"}), request_id=uuid4(), input="question"
    )
    assert created.submission is not None
    await asyncio.wait_for(started.wait(), 5)
    second = await service.submit_input(
        created.session.id, "second question", request_id=uuid4(), mode="steer"
    )
    ready.set()
    async with asyncio.timeout(5):
        while not await database.rows(  # noqa: ASYNC110 - bounded final State observation
            "SELECT 1 FROM runs WHERE id=%s AND status='failed'", (run_id,)
        ):
            await asyncio.sleep(0.01)
    errors = await database.rows("SELECT data FROM records WHERE kind='error'")
    assert len(errors) == 1 and errors[0]["data"]["kind"] == "Conflict"
    assert not await database.rows("SELECT 1 FROM records WHERE kind='final'")
    for index, submission in enumerate((created.submission, second)):
        status = await service.wait_submission(created.session.id, submission.request_id)
        assert status.completion is not None
        committed = invalid_final == "older" or invalid_final == "partial" and index == 0
        assert status.completion.outcome == ("completed" if committed else "failed")
        assert status.completion.output == (
            ReplyTo((submission.waiting_id,), "first answer" if index == 0 else "second answer")
            if committed
            else None
        )


async def test_partial_reply_keeps_existing_wait_active_for_running_producer(database: Database):
    dependency = uuid4()
    replied, published = asyncio.Event(), asyncio.Event()
    received = []
    runs = []

    async def runner(ctx):
        runs.append(ctx.run_id)
        await ctx.checkpoint(
            CheckpointWrite(
                ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in ctx.inputs)
            )
        )
        if ctx.inputs[0].payload == "begin":
            return RunResult(
                WaitFor((dependency,)),
                CheckpointWrite(ctx.checkpoint_number + 1, ctx.state, (), ()),
            )
        addresses = (await ctx.unreplied_addresses()).being_waited_ids
        receipt = await ctx.reply(
            emission_id=uuid4(), output=ReplyTo(addresses[:1], "first answer")
        )
        assert receipt.remaining_being_waited_ids == addresses[1:]
        replied.set()
        await published.wait()
        inputs = await ctx.poll_steer()
        received.extend(inputs)
        assert len(inputs) == 1
        assert inputs[0].event_id == dependency and inputs[0].mode == "steer"
        assert inputs[0].being_waited_id is None
        assert inputs[0].payload["output"] == "dependency result"
        await ctx.checkpoint(
            CheckpointWrite(ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in inputs))
        )
        final = await ctx.reply(
            emission_id=uuid4(),
            output=ReplyTo(receipt.remaining_being_waited_ids, "dependency handled"),
        )
        return RunResult(
            final.output, CheckpointWrite(ctx.checkpoint_number + 1, ctx.state, (), ())
        )

    service = await database.start(runner)
    created = await service.create_session(
        replace(spec(), config={"output_mode": "reply_to"}), request_id=uuid4(), input="begin"
    )
    await waiting(database, created.session.id)
    continued = await service.submit_input(created.session.id, "continue", request_id=uuid4())
    await asyncio.wait_for(replied.wait(), 5)
    assert (await service.get_session(created.session.id)).status == "running"
    assert await database.rows(
        "SELECT 1 FROM waiting_channels WHERE id=%s AND active", (dependency,)
    )
    await service.publish_event(
        dependency, "dependency result", request_id=uuid4(), producer_session_id=None
    )
    published.set()
    status = await service.wait_submission(created.session.id, continued.request_id, wait_seconds=5)
    assert status.completion is not None and status.completion.output == ReplyTo(
        (continued.waiting_id,), "dependency handled"
    )
    await waiting(database, created.session.id)
    assert len(received) == 1 and len(runs) == 2


@pytest.mark.parametrize(
    "body_size,call_id,rejected",
    [
        (121800, "call1", True),
        (121300, "call1", False),
        (121300, "c" * 1024, True),
    ],
)
async def test_reply_checks_actual_tool_return_before_committing(
    database, body_size, call_id, rejected
):
    started, seeded, finished = (asyncio.Event() for _ in range(3))
    captured = None
    selected = None
    calls = 0
    channel = uuid4()

    async def handle(request):
        nonlocal calls
        assert selected is not None
        calls += 1
        body = json.loads(request.content)
        if calls == 1:
            return response(
                text="x" * body_size,
                name="reply_to",
                args={"ids": [str(selected.waiting_id)]},
                call_id=call_id,
            )
        status = await service.wait_submission(selected.session_id, selected.request_id)
        if rejected and calls == 2:
            assert status.completion is None
            assert not await database.rows("SELECT 1 FROM records WHERE kind='reply'")
            assert any(
                m["role"] == "tool" and "exceeds" in m.get("content", "") for m in body["messages"]
            )
            return response(
                text="Fits the complete envelope",
                name="reply_to",
                args={"ids": [str(selected.waiting_id)]},
                call_id="corrected",
            )
        assert status.completion is not None and status.completion.outcome == "completed"
        return response(name="wait_for", args={"ids": [str(channel)]}, call_id="wait")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = ai_runner(client)

        async def run(ctx):
            nonlocal captured
            captured = ctx
            started.set()
            await seeded.wait()
            result = await agent(ctx)
            finished.set()
            return result

        service = await database.start(run)
        created = await service.create_session(
            replace(
                spec(),
                config={"output_mode": "reply_to"},
                initial_state=agent.initial_state(instructions="", skills=[]),
            ),
            request_id=uuid4(),
            input="Selected question",
        )
        selected = created.submission
        assert selected is not None
        await asyncio.wait_for(started.wait(), 5)
        assert captured is not None
        # Model a persisted backlog from previous work without thousands of unrelated RPCs.
        await database.rows(
            "WITH seeded AS ("
            "INSERT INTO inputs(id,session_id,mode,payload,seq,run_id,state,being_waited_id) "
            "SELECT gen_random_uuid(),%s,'queue','\"Earlier question\"'::jsonb,g+100,%s,'consumed',"
            "gen_random_uuid() FROM generate_series(1,3500) g "
            "RETURNING id,session_id,being_waited_id),"
            "submitted AS (INSERT INTO requests("
            "id,operation,fingerprint,target_session_id,input_id,waiting_id) "
            "SELECT gen_random_uuid(),'session.input','fixture',session_id,id,being_waited_id "
            "FROM seeded "
            "RETURNING id,waiting_id,target_session_id) "
            "INSERT INTO waiting_channels(id,request_id,producer_session_id) "
            "SELECT waiting_id,id,target_session_id FROM submitted RETURNING id",
            (created.session.id, captured.run_id),
        )
        seeded.set()
        await asyncio.wait_for(finished.wait(), 15)
        assert calls == (3 if rejected else 2)
        replies = await database.rows("SELECT data FROM records WHERE kind='reply'")
        assert len(replies) == 1
        assert len(replies[0]["data"]["remaining_being_waited_ids"]) == 3500
        assert replies[0]["data"]["output"]["payload"] == (
            "Fits the complete envelope" if rejected else "x" * body_size
        )

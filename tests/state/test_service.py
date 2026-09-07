import asyncio
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from kapy.state import (
    CheckpointWrite,
    Conflict,
    InvalidArgument,
    MessageWrite,
    NotFound,
    OutputDelta,
    RunContext,
    RunnerState,
    RunResult,
    ServiceUnavailable,
    SessionService,
    migrate,
)

from .conftest import DATABASE_URL, VALKEY_URL, Database, spec

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def result(ctx: RunContext, output: str = "done", waits: tuple[UUID, ...] = ()) -> RunResult:
    return RunResult(
        output,
        waits,
        CheckpointWrite(ctx.checkpoint_number + 1, ctx.state, (), tuple(i.id for i in ctx.inputs)),
    )


async def simple(ctx: RunContext) -> RunResult:
    return result(ctx)


async def test_crud_receipts_and_migration(database: Database) -> None:
    await migrate(DATABASE_URL, schema=database.schema)
    service = await database.start(simple)
    key = uuid4()
    created = await service.create_session(spec(), request_id=key)
    assert created.session.status == "waiting"
    assert created.session.machine_ids == ("machine-a", "machine-b")
    replay = await service.create_session(
        replace(spec(), initial_state=RunnerState("new-codec", {})), request_id=key
    )
    assert replay == created
    with pytest.raises(Conflict):
        await service.create_session(spec("changed"), request_id=key)
    changed = await service.update_session(
        created.session.id,
        title="renamed",
        machine_ids=("b",),
        default_machine_id="b",
        config={"model": "fake"},
    )
    assert changed.title == "renamed"
    assert changed.default_machine_id == "b"
    assert (await service.list_sessions(session_ids=())).items == ()
    assert len((await service.list_sessions(session_ids=(changed.id,))).items) == 1
    with pytest.raises(InvalidArgument):
        await service.update_session(
            changed.id, title="x", machine_ids=(), default_machine_id="missing", config={}
        )
    assert await service.delete_session(changed.id)
    assert not await service.delete_session(changed.id)
    with pytest.raises(NotFound):
        await service.get_session(changed.id)
    assert await service.create_session(spec(), request_id=key) == created


async def test_concurrent_sessions_and_steer_queue(database: Database) -> None:
    starts: asyncio.Queue[RunContext] = asyncio.Queue()
    poll = asyncio.Event()
    finish = asyncio.Event()
    runs: dict[UUID, list[list[str]]] = {}
    active: set[UUID] = set()
    saw_steer = asyncio.Event()

    async def runner(ctx: RunContext) -> RunResult:
        assert ctx.session.id not in active
        active.add(ctx.session.id)
        runs.setdefault(ctx.session.id, []).append([str(i.payload) for i in ctx.inputs])
        await starts.put(ctx)
        if ctx.inputs[0].payload == "first":
            await poll.wait()
            steer = await ctx.poll_steer()
            runs[ctx.session.id][-1].extend(str(i.payload) for i in steer)
            await ctx.checkpoint(
                CheckpointWrite(
                    1,
                    RunnerState("fake-v1", {"saved": True}),
                    (),
                    tuple(i.id for i in (*ctx.inputs, *steer)),
                )
            )
            saw_steer.set()
            await finish.wait()
            consumed = ()
        else:
            consumed = tuple(i.id for i in ctx.inputs)
        active.remove(ctx.session.id)
        return RunResult(
            "done", (), CheckpointWrite(ctx.checkpoint_number + 1, ctx.state, (), consumed)
        )

    service = await database.start(runner)
    first = await service.create_session(spec(), request_id=uuid4(), input="first")
    ctx = await asyncio.wait_for(starts.get(), 5)
    second = await service.create_session(spec(), request_id=uuid4(), input="other")
    await database.completed(second.submission.request_id)
    assert ctx.session.id in active  # second session finished while first is still running
    queue = await service.submit_input(first.session.id, "queue", request_id=uuid4(), mode="queue")
    steer = await service.submit_input(first.session.id, "steer", request_id=uuid4())
    poll.set()
    await asyncio.wait_for(saw_steer.wait(), 5)
    late = await service.submit_input(first.session.id, "late", request_id=uuid4())
    assert (
        await database.rows("SELECT completion FROM requests WHERE id=%s", (queue.request_id,))
    )[0]["completion"] is None
    finish.set()
    for submission in (first.submission, queue, steer, late):
        assert (await database.completed(submission.request_id))["outcome"] == "completed"
    assert runs[first.session.id] == [["first", "steer"], ["queue", "late"]]
    assert len({(await database.completed(s.request_id))["run_id"] for s in (queue, late)}) == 1
    assert (await database.completed(first.submission.request_id))["run_id"] != (
        await database.completed(queue.request_id)
    )["run_id"]


async def test_checkpoint_output_idempotency_and_cursor(database: Database) -> None:
    release = asyncio.Event()
    emitted = asyncio.Event()
    ctx_holder: list[RunContext] = []

    async def runner(ctx: RunContext) -> RunResult:
        ctx_holder.append(ctx)
        message_id = uuid4()
        delta = OutputDelta(uuid4(), message_id, "text_delta", "hel")
        first = await ctx.emit(delta)
        assert await ctx.emit(delta) == first
        with pytest.raises(Conflict):
            await ctx.emit(replace(delta, data="different"))
        checkpoint = CheckpointWrite(
            1,
            RunnerState("fake-v1", {"seen": True}),
            (MessageWrite(message_id, "model_response", "hello", {"text": "hello"}),),
            tuple(i.id for i in ctx.inputs),
        )
        saved = await ctx.checkpoint(checkpoint)
        assert await ctx.checkpoint(checkpoint) == saved
        with pytest.raises(Conflict):
            await ctx.checkpoint(replace(checkpoint, state=RunnerState("x", {})))
        emitted.set()
        await release.wait()
        return RunResult("hello", (), CheckpointWrite(2, ctx.state, (), ()))

    service = await database.start(runner)
    created = await service.create_session(spec(), request_id=uuid4(), input="hello")
    await asyncio.wait_for(emitted.wait(), 5)
    page = await service.read_output(created.session.id, limit=1)
    observed = list(page.items)
    while page.has_more:
        page = await service.read_output(created.session.id, after=page.next_cursor, limit=1)
        observed.extend(page.items)
    assert [item.kind for item in observed] == ["input", "text_delta", "model_response"]
    waiting = asyncio.create_task(
        service.read_output(created.session.id, after=page.next_cursor, wait_seconds=5)
    )
    release.set()
    live = await waiting
    assert [item.kind for item in live.items] == ["final", "waiting"]
    await database.completed(created.submission.request_id)
    with pytest.raises(Conflict):
        await ctx_holder[0].emit(OutputDelta(uuid4(), uuid4(), "notice", "late"))
    other = await service.create_session(spec(), request_id=uuid4())
    with pytest.raises(InvalidArgument):
        await service.read_output(other.session.id, after=live.next_cursor)
    assert (await service.read_output(created.session.id, after=live.next_cursor)).items == ()


async def test_restart_reserves_only_unconfirmed_inputs(database: Database) -> None:
    ready = asyncio.Event()
    first_run: list[RunContext] = []
    delivered: list[tuple[bool, list[str], dict]] = []

    async def interrupted(ctx: RunContext) -> RunResult:
        first_run.append(ctx)
        await ctx.checkpoint(
            CheckpointWrite(
                1, RunnerState("fake-v1", {"saved": "initial"}), (), tuple(i.id for i in ctx.inputs)
            )
        )
        ready.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    service = await database.start(interrupted)
    created = await service.create_session(spec(), request_id=uuid4(), input="initial")
    await asyncio.wait_for(ready.wait(), 5)
    pending = await service.submit_input(
        created.session.id, "next", request_id=uuid4(), mode="queue"
    )
    await service.__aexit__(None, None, None)

    async def resume(ctx: RunContext) -> RunResult:
        delivered.append((ctx.recovered, [str(i.payload) for i in ctx.inputs], ctx.state.data))
        if ctx.recovered:
            assert ctx.run_id == first_run[0].run_id
            assert ctx.attempt == 2
            assert ctx.checkpoint_number == 1
        return result(ctx)

    reopened = await database.start(resume)
    await database.completed(created.submission.request_id)
    await database.completed(pending.request_id)
    assert delivered == [(True, [], {"saved": "initial"}), (False, ["next"], {"saved": "initial"})]
    page = await reopened.read_output(created.session.id)
    assert sum(item.kind == "interrupted" for item in page.items) == 1


async def test_failed_runner_and_delete_release_pending_work(database: Database) -> None:
    gate = asyncio.Event()
    started = asyncio.Event()

    async def runner(ctx: RunContext) -> RunResult:
        if ctx.inputs[0].payload == "fail":
            started.set()
            await gate.wait()
            raise RuntimeError("do not persist secret string")
        if ctx.inputs[0].payload == "block":
            started.set()
            await asyncio.Event().wait()
        return result(ctx)

    service = await database.start(runner)
    first = await service.create_session(spec(), request_id=uuid4(), input="fail")
    await asyncio.wait_for(started.wait(), 5)
    queued = await service.submit_input(first.session.id, "okay", request_id=uuid4(), mode="queue")
    gate.set()
    assert (await database.completed(first.submission.request_id))["outcome"] == "failed"
    assert (await database.completed(queued.request_id))["outcome"] == "completed"
    assert "secret" not in str((await service.read_output(first.session.id)).items)
    started.clear()
    blocked = await service.create_session(spec(), request_id=uuid4(), input="block")
    await asyncio.wait_for(started.wait(), 5)
    assert await service.delete_session(blocked.session.id)
    assert (await database.completed(blocked.submission.request_id))["outcome"] == "deleted"
    assert not await database.rows(
        "SELECT * FROM subscriptions WHERE session_id=%s", (blocked.session.id,)
    )


async def test_lease_and_lost_valkey_hints(database: Database) -> None:
    service = await database.start(simple, valkey_url="redis://127.0.0.1:1/0")
    created = await service.create_session(spec(), request_id=uuid4(), input="durable")
    assert (await database.completed(created.submission.request_id))["outcome"] == "completed"
    with pytest.raises(Conflict):
        async with SessionService(
            database_url=DATABASE_URL, valkey_url=VALKEY_URL, runner=simple, schema=database.schema
        ):
            raise AssertionError("second owner must not start")
    assert service._store.lease is not None
    await service._store.lease.close()
    async with asyncio.timeout(5):
        while service._available:  # noqa: ASYNC110 - observe lease monitor
            await asyncio.sleep(0.02)
    with pytest.raises(ServiceUnavailable):
        await service.create_session(spec(), request_id=uuid4())

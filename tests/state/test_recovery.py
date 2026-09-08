import asyncio
import json
import os
import sys
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from kapy.state import CheckpointWrite, Conflict, RunContext, RunnerState, RunResult, migrate

from .conftest import DATABASE_URL, VALKEY_URL, Database, spec
from .test_service import result, simple

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_abrupt_control_process_exit_recovers_committed_checkpoint(
    database: Database,
) -> None:
    worker = """
import asyncio, json, os, sys
from uuid import uuid4
from kapy.state import SessionService, SessionSpec, RunnerState, CheckpointWrite
async def runner(ctx):
    await ctx.checkpoint(CheckpointWrite(1, RunnerState("fake-v1", {"survived": True}), (),
                                       tuple(i.id for i in ctx.inputs)))
    print(json.dumps({"session_id": str(ctx.session.id), "run_id": str(ctx.run_id)}), flush=True)
    await asyncio.Event().wait()
async def main():
    async with SessionService(database_url=os.environ["KAPY_DATABASE_URL"],
            valkey_url=os.environ["KAPY_VALKEY_URL"], runner=runner,
            schema=sys.argv[1], namespace=sys.argv[1]) as service:
        await service.create_session(SessionSpec("crash", (), None, {}, RunnerState("fake-v1", {})),
                                     request_id=uuid4(), input="once")
        await asyncio.Event().wait()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        worker,
        database.schema,
        env={**os.environ, "KAPY_DATABASE_URL": DATABASE_URL, "KAPY_VALKEY_URL": VALKEY_URL},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        line = await asyncio.wait_for(process.stdout.readline(), 10)
        identity = json.loads(line)
    finally:
        if process.returncode is None:
            process.kill()  # only this dedicated test control process
        await process.wait()
    completed = asyncio.Event()

    async def resume(ctx: RunContext) -> RunResult:
        assert str(ctx.run_id) == identity["run_id"]
        assert ctx.recovered and ctx.attempt == 2
        assert ctx.state.data == {"survived": True}
        assert ctx.inputs == ()
        completed.set()
        return result(ctx)

    await database.start(resume)
    await asyncio.wait_for(completed.wait(), 10)
    rows = await database.rows(
        "SELECT id FROM requests WHERE target_session_id=%s", (identity["session_id"],)
    )
    assert (await database.completed(rows[0]["id"]))["outcome"] == "completed"


async def test_lost_all_wakeups_periodic_scan_finds_work(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = await database.start(simple, valkey_url="redis://127.0.0.1:1/0")
    # Suppress both the local immediate wake and network notification for this commit.
    monkeypatch.setattr(service, "_signal", lambda: None)
    created = await service.create_session(spec(), request_id=uuid4(), input="periodic scan")
    assert created.submission is not None
    assert (await database.completed(created.submission.request_id))["outcome"] == "completed"


async def test_final_transaction_rolls_back_before_failure_completion(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def runner(ctx: RunContext) -> RunResult:
        return RunResult(
            "should roll back",
            CheckpointWrite(
                1,
                RunnerState("new-state", {"uncommitted": True}),
                (),
                tuple(i.id for i in ctx.inputs),
            ),
        )

    service = await database.start(runner)
    original = service._completion
    injected = False

    async def event(*args, **kwargs):
        nonlocal injected
        if not injected:
            injected = True
            raise RuntimeError("injected after final checkpoint, before waiting commits")
        return await original(*args, **kwargs)

    monkeypatch.setattr(service, "_completion", event)
    created = await service.create_session(spec(), request_id=uuid4(), input="go")
    assert created.submission is not None
    assert (await database.completed(created.submission.request_id))["outcome"] == "failed"
    records = await service.read_output(created.session.id)
    assert [record.kind for record in records.items] == ["input", "error"]
    assert not await database.rows("SELECT * FROM checkpoints")
    assert (await database.rows("SELECT runner_state FROM runs"))[0]["runner_state"][
        "codec"
    ] == "fake-v1"


async def test_migration_checksum_and_epoch_fencing(database: Database) -> None:
    await database.rows("UPDATE schema_migrations SET checksum=%s RETURNING version", ("tampered",))
    with pytest.raises(Conflict):
        await migrate(DATABASE_URL, schema=database.schema)
    # Startup uses installed schema, but every write checks the durable epoch.
    service = await database.start(simple)
    await database.rows("UPDATE service_meta SET epoch=%s RETURNING epoch", (uuid4(),))
    from kapy.state import ServiceUnavailable

    with pytest.raises(ServiceUnavailable):
        await service.create_session(spec(), request_id=uuid4())


async def test_nested_session_configuration_is_read_back_unchanged(database: Database) -> None:
    service = await database.start(simple)
    created = await service.create_session(spec(), request_id=uuid4())
    uncommon = {"future_option": {"nested": [1, None, "中文", {"enabled": True}]}}
    await database.rows(
        "UPDATE sessions SET config=%s WHERE id=%s RETURNING id",
        (Jsonb(uncommon), created.session.id),
    )
    assert (await service.get_session(created.session.id)).config == uncommon

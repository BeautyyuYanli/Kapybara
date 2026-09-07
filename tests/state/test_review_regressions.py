"""Behavioral repros from staged review and approved Gateway integration additions."""

import asyncio
import json
from dataclasses import asdict
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from kapy.state import (
    CheckpointWrite,
    Conflict,
    HistoryExportPage,
    InvalidArgument,
    JsonObject,
    JsonValue,
    NotFound,
    RecordPage,
    RunContext,
    RunResult,
    ServiceUnavailable,
    SessionPage,
)

from .conftest import Database, spec
from .test_service import result, simple

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def wire_size(value: RecordPage | HistoryExportPage | SessionPage) -> int:
    def default(item: object) -> object:
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, datetime):
            return item.isoformat()
        raise TypeError(type(item).__name__)

    return len(json.dumps(asdict(value), default=default, ensure_ascii=True).encode("utf-8"))


async def blocked(ctx: RunContext) -> RunResult:
    await asyncio.Event().wait()
    raise AssertionError("unreachable")


async def test_delete_13200_publicly_accepted_inputs_completes_every_receipt(
    database: Database,
) -> None:
    service = await database.start(blocked)
    created = await service.create_session(spec(), request_id=uuid4())
    session_id = created.session.id
    request_ids = [uuid4() for _ in range(13_200)]
    # Using the default channel also verifies that batching retains every request identity there.
    for request_id in request_ids:
        await service.submit_input(session_id, "x", request_id=request_id, waiting_id=session_id)
    delete_id = uuid4()
    assert await service.delete_session(session_id, request_id=delete_id)
    assert await service.delete_session(session_id, request_id=delete_id)
    with pytest.raises(NotFound):
        await service.get_session(session_id)
    rows = await database.rows(
        "SELECT id FROM requests WHERE operation='input' AND completion->>'outcome'='deleted'"
    )
    assert {row["id"] for row in rows} == set(request_ids)
    notifications = await database.rows(
        "SELECT payload FROM events WHERE channel_id=%s "
        "AND payload->>'outcome'='deleted' ORDER BY ordinal",
        (session_id,),
    )
    emitted = [key for row in notifications for key in row["payload"]["request_ids"]]
    assert all(len(row["payload"]["request_ids"]) <= 64 for row in notifications)
    assert len(emitted) == len(set(emitted)) == 13_200
    assert set(emitted) == {str(key) for key in request_ids}
    for request_id in (request_ids[0], request_ids[-1]):
        observed = await service.wait_submission(session_id, request_id)
        assert observed.completion is not None and observed.completion.outcome == "deleted"


@pytest.mark.parametrize("error_type", [None, NotFound, ServiceUnavailable])
async def test_many_steer_requests_complete_once_even_for_runner_state_errors(
    database: Database,
    error_type: type[Exception] | None,
) -> None:
    release = asyncio.Event()
    entered = asyncio.Event()
    calls = 0

    async def runner(ctx: RunContext) -> RunResult:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        ids = [item.id for item in ctx.inputs]
        while batch := await ctx.poll_steer():
            ids.extend(item.id for item in batch)
        if error_type:
            raise error_type("raised by the injected runner, not the State lease")
        return RunResult("all done", (), CheckpointWrite(1, ctx.state, (), tuple(ids)))

    service = await database.start(runner)
    created = await service.create_session(spec(), request_id=uuid4(), input="initial")
    await asyncio.wait_for(entered.wait(), 5)
    request_ids = [created.submission.request_id]
    for _ in range(193):
        submission = await service.submit_input(
            created.session.id, "more", request_id=uuid4(), waiting_id=created.session.id
        )
        request_ids.append(submission.request_id)
    release.set()
    observed = await service.wait_submission(created.session.id, request_ids[-1], wait_seconds=10)
    assert observed.completion is not None
    assert observed.completion.outcome == ("failed" if error_type else "completed")
    assert calls == 1
    rows = await database.rows("SELECT completion FROM requests WHERE id=ANY(%s)", (request_ids,))
    assert len(rows) == 194 and all(row["completion"] is not None for row in rows)
    assert len({row["completion"]["run_id"] for row in rows}) == 1
    page = await service.read_output(created.session.id)
    waiting = [item for item in page.items if item.kind == "waiting"]
    while page.has_more:
        page = await service.read_output(created.session.id, after=page.next_cursor)
        waiting.extend(item for item in page.items if item.kind == "waiting")
    emitted: list[str] = []
    for item in waiting:
        assert isinstance(item.data, dict)
        keys = item.data["request_ids"]
        assert isinstance(keys, list)
        assert len(keys) <= 64
        emitted.extend(str(key) for key in keys)
    assert len(emitted) == len(set(emitted)) == len(request_ids)
    assert set(emitted) == {str(k) for k in request_ids}


async def test_terminal_page_actual_json_size_and_snapshot_export(database: Database) -> None:
    service = await database.start(blocked)
    created = await service.create_session(spec(), request_id=uuid4())
    payloads: list[JsonValue] = [
        "x" * 130_000,
        "x" * 130_000,
        {"type": "session.waiting", "output": "x" * 1748, "x": 10},
    ]
    for payload in payloads:
        await service.submit_input(created.session.id, payload, request_id=uuid4())
    for reader in (service.read_output, service.read_history, service.export_history):
        page = await reader(created.session.id, after=created.session.cursor, limit=3)
        assert page.has_more and len(page.items) == 2
        assert wire_size(page) <= 512 * 1024
        received = [item.data for item in page.items]
        while page.has_more:
            if isinstance(page, HistoryExportPage):
                page = await service.export_history(
                    created.session.id,
                    after=page.next_cursor,
                    snapshot=page.snapshot_cursor,
                    limit=3,
                )
            else:
                page = await reader(created.session.id, after=page.next_cursor, limit=3)
            assert wire_size(page) <= 512 * 1024
            received.extend(item.data for item in page.items)
        assert received == payloads
    page = await service.search_history(
        created.session.id, "", mode="substring", after=created.session.cursor, limit=3
    )
    assert page.has_more and wire_size(page) <= 512 * 1024


async def test_snapshot_excludes_later_appends_and_scopes_all_cursors(database: Database) -> None:
    service = await database.start(blocked)
    created = await service.create_session(spec(), request_id=uuid4())
    for payload in ("before 1", "before 2"):
        await service.submit_input(created.session.id, payload, request_id=uuid4())
    page = await service.export_history(created.session.id, limit=1)
    snapshot = page.snapshot_cursor
    initial = list(page.items)
    await service.submit_input(created.session.id, "after snapshot", request_id=uuid4())
    while page.has_more:
        page = await service.export_history(
            created.session.id, after=page.next_cursor, snapshot=snapshot, limit=1
        )
        assert page.snapshot_cursor == snapshot
        initial.extend(page.items)
    assert [item.data for item in initial if item.kind == "input"] == ["before 1", "before 2"]
    assert page.next_cursor == snapshot
    fresh = await service.export_history(created.session.id)
    assert [item.data for item in fresh.items if item.kind == "input"][-1] == "after snapshot"
    with pytest.raises(InvalidArgument):
        await service.export_history(created.session.id, after=fresh.next_cursor, snapshot=snapshot)
    other = await service.create_session(spec(), request_id=uuid4())
    with pytest.raises(InvalidArgument):
        await service.export_history(other.session.id, snapshot=snapshot)
    await service.delete_session(created.session.id, request_id=uuid4())
    with pytest.raises(NotFound):
        await service.export_history(created.session.id, snapshot=snapshot)


async def test_update_and_delete_retries_keep_the_original_result(database: Database) -> None:
    service = await database.start(blocked)
    created = await service.create_session(spec(), request_id=uuid4())
    update_id = uuid4()
    config: JsonObject = {"v": 1}
    args = {"title": "first", "machine_ids": (), "default_machine_id": None, "config": config}
    first = await service.update_session(created.session.id, request_id=update_id, **args)
    await service.update_session(
        created.session.id, request_id=uuid4(), **{**args, "title": "later"}
    )
    assert await service.update_session(created.session.id, request_id=update_id, **args) == first
    assert (await service.get_session(created.session.id)).title == "later"
    await service.submit_input(created.session.id, "run", request_id=uuid4())
    assert await service.update_session(created.session.id, request_id=update_id, **args) == first
    with pytest.raises(Conflict):
        await service.update_session(
            created.session.id, request_id=update_id, **{**args, "title": "wrong"}
        )
    delete_id = uuid4()
    assert await service.delete_session(created.session.id, request_id=delete_id)
    assert await service.update_session(created.session.id, request_id=update_id, **args) == first
    assert await service.delete_session(created.session.id, request_id=delete_id)
    missing_id = uuid4()
    assert not await service.delete_session(created.session.id, request_id=missing_id)
    assert not await service.delete_session(created.session.id, request_id=missing_id)
    with pytest.raises(Conflict):
        await service.delete_session(uuid4(), request_id=delete_id)


async def test_delete_intent_recovers_and_preserves_first_result(
    database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = await database.start(blocked)
    created = await service.create_session(spec(), request_id=uuid4(), input="pending")
    entered = asyncio.Event()

    async def pause_delete(session_id: UUID) -> None:
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(service, "_finish_delete", pause_delete)
    delete_id = uuid4()
    task = asyncio.create_task(service.delete_session(created.session.id, request_id=delete_id))
    await asyncio.wait_for(entered.wait(), 5)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    await service.__aexit__(None, None, None)
    reopened = await database.start(simple)
    assert await reopened.delete_session(created.session.id, request_id=delete_id)
    assert await reopened.delete_session(created.session.id, request_id=delete_id)
    observed = await reopened.wait_submission(created.session.id, created.submission.request_id)
    assert observed.completion is not None and observed.completion.outcome == "deleted"
    with pytest.raises(NotFound):
        await reopened.get_session(created.session.id)


async def test_receipt_wait_is_nonconsuming_repeatable_and_closes_promptly(
    database: Database,
) -> None:
    release = asyncio.Event()

    async def runner(ctx: RunContext) -> RunResult:
        await release.wait()
        return result(ctx, "receipt output")

    service = await database.start(runner)
    created = await service.create_session(spec(), request_id=uuid4(), input="work")
    status = await service.wait_submission(
        created.session.id, created.submission.request_id, wait_seconds=0.01
    )
    assert status.completion is None and status.submission == created.submission
    for target, key in ((uuid4(), created.submission.request_id), (created.session.id, uuid4())):
        with pytest.raises(NotFound):
            await service.wait_submission(target, key)
    observers = [
        asyncio.create_task(
            service.wait_submission(
                created.session.id, created.submission.request_id, wait_seconds=10
            )
        )
        for _ in range(2)
    ]
    release.set()
    first, second = await asyncio.gather(*observers)
    assert first == second and first.completion is not None
    assert first.completion.output == "receipt output"
    events = await database.rows("SELECT id,state FROM events ORDER BY ordinal")
    subscriptions = await database.rows(
        "SELECT * FROM subscriptions ORDER BY channel_id,session_id"
    )
    assert await service.wait_submission(created.session.id, created.submission.request_id) == first
    assert await database.rows("SELECT id,state FROM events ORDER BY ordinal") == events
    assert (
        await database.rows("SELECT * FROM subscriptions ORDER BY channel_id,session_id")
        == subscriptions
    )
    await service.__aexit__(None, None, None)
    reopened = await database.start(blocked)
    assert (
        await reopened.wait_submission(created.session.id, created.submission.request_id) == first
    )
    pending = await reopened.submit_input(created.session.id, "block", request_id=uuid4())
    waiter = asyncio.create_task(
        reopened.wait_submission(created.session.id, pending.request_id, wait_seconds=30)
    )
    await asyncio.sleep(0.01)
    await reopened.__aexit__(None, None, None)
    with pytest.raises(ServiceUnavailable):
        await asyncio.wait_for(waiter, 2)


async def test_list_pages_with_large_configs_fit_the_final_wire_shape(database: Database) -> None:
    service = await database.start(simple)
    expected = set()
    for i in range(5):
        original = spec(str(i))
        from dataclasses import replace

        created = await service.create_session(
            replace(original, config={"large": "x" * 130_000}), request_id=uuid4()
        )
        expected.add(created.session.id)
    page = await service.list_sessions()
    observed = {item.id for item in page.items}
    assert page.next_after is not None
    assert wire_size(page) <= 512 * 1024
    while page.next_after:
        page = await service.list_sessions(after=page.next_after)
        assert wire_size(page) <= 512 * 1024
        observed.update(item.id for item in page.items)
    assert observed == expected

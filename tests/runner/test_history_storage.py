"""Real PostgreSQL atomic checkpoints, sealed history and post-commit broadcasts."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage
from sqlalchemy import event

from kapy.agent_history import HistoryMessage, HistoryRepository
from kapy.agent_history.models import AgentHistoryRow
from kapy.agent_output import MessageCommitted
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.control.sessions.types import OwnershipLost
from kapy.lifecycle import LifecycleStatus
from kapy.runner.checkpoints import (
    Checkpoint,
    DoneNext,
    FinishNext,
    ModelRequestNext,
    decode_state,
    validate_history,
)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def checkpoint(deps, messages, *, done=False):
    await deps.recorder.record_messages(
        deps,
        messages,
        checkpoint="run_completed" if done else "before_model_request",
        next=DoneNext(output="done") if done else ModelRequestNext(),
        release=done,
    )


def history(session_id, seq, content="message"):
    return HistoryMessage(session_id, seq, ModelRequest([UserPromptPart(content)]))


async def test_history_snapshot_state_and_release_use_one_transaction(runner_database):
    deps = runner_database.deps()
    config = {"cwd": "/tmp"}
    state = {"scope_id": str(deps.session_id), "jobs": {"job": 7}}
    deps.layers = {"shell": {"config": config, "state": deepcopy(state)}}
    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, deps.session_id)
        row.layers = {"shell": config}
    messages: list[ModelMessage] = [
        ModelRequest([UserPromptPart("input")], metadata={"business": "input"})
    ]
    async with deps.output_service.subscribe(deps.session_id) as events:
        await checkpoint(deps, messages)
        assert await anext(events) == [
            MessageCommitted(HistoryMessage(deps.session_id, 0, messages[0]))
        ]
    messages.append(
        ModelResponse(
            [TextPart("answer")],
            usage=RequestUsage(
                input_tokens=100, output_tokens=20, cache_read_tokens=50, details={"extra": 7}
            ),
            finish_reason="stop",
        )
    )
    await deps.recorder.record_messages(
        deps, messages, checkpoint="tools_node_completed", next=FinishNext(output="answer")
    )
    deps.layers["shell"]["state"]["jobs"]["job"] = 9
    await checkpoint(deps, messages, done=True)
    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, deps.session_id)
        saved = decode_state(row.runner_state)
        entries = await HistoryRepository(db).read_history_entries(deps.session_id)
        assert isinstance(saved, Checkpoint) and isinstance(saved.next, DoneNext)
        validate_history(saved, entries)
        assert row.layer_states["shell"]["jobs"] == {"job": 9}
        assert row.status == LifecycleStatus.READY
        assert row.current_activity_id is row.current_activity_run_id is None
        assert isinstance(saved.messages[-1], ModelResponse)
        assert saved.messages[-1].usage.cache_read_tokens == 50
        assert isinstance(entries[-1].message, ModelResponse)
        assert entries[-1].message.usage.cache_read_tokens == 0
        assert [e.seq for e in entries] == [0, 1]


@pytest.mark.parametrize("failure", ["save", "accepted", "codec", "state"])
async def test_checkpoint_failure_rolls_back_history_state_and_live_metadata(
    runner_database, monkeypatch, failure
):
    deps = runner_database.deps()
    messages = [ModelRequest([UserPromptPart("input")])]

    if failure == "save":
        save = SessionRepository._save_checkpoint

        async def fail_save(self, *args, **kwargs):
            await save(self, *args, **kwargs)
            raise ValueError("failure")

        monkeypatch.setattr(SessionRepository, "_save_checkpoint", fail_save)
    elif failure == "accepted":
        append = HistoryRepository.append_history

        async def reject_part(self, *args):
            await append(self, *args)
            return ()

        monkeypatch.setattr(HistoryRepository, "append_history", reject_part)
    elif failure == "codec":

        def fail_codec(*args, **kwargs):
            raise ValueError("failure")

        monkeypatch.setattr(Checkpoint, "model_dump_json", fail_codec)
    else:
        deps.layers = {"shell": {"config": {"cwd": "/tmp"}, "state": {"jobs": {}}}}
    with pytest.raises(ValueError):
        await checkpoint(deps, messages)
    assert messages[0].metadata is None and deps.recorder.boundary == -1
    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, deps.session_id)
        assert row.runner_state is None and row.layer_states == {}
        assert row.current_activity_id == deps.activity_id
        assert await HistoryRepository(db).read_history_entries(deps.session_id) == ()


async def test_empty_suffix_still_saves_checkpoint_and_state(runner_database):
    deps = runner_database.deps()
    messages = [ModelRequest([UserPromptPart("input")])]
    await checkpoint(deps, messages)
    async with runner_database.sessions.begin() as db:
        created_at = (await db.get(AgentHistoryRow, (deps.session_id, 0))).created_at
    await deps.recorder.record_messages(
        deps, messages, checkpoint="tools_node_completed", next=FinishNext(output="candidate")
    )
    async with runner_database.sessions.begin() as db:
        saved = decode_state((await db.get(SessionRow, deps.session_id)).runner_state)
        assert isinstance(saved, Checkpoint) and isinstance(saved.next, FinishNext)
        assert (await db.get(AgentHistoryRow, (deps.session_id, 0))).created_at == created_at


async def test_failed_broadcast_keeps_acknowledged_seq_and_can_continue(
    runner_database, monkeypatch
):
    deps = runner_database.deps()
    messages: list[ModelMessage] = [ModelRequest([UserPromptPart("input")])]

    def fail_publish(*args):
        raise RuntimeError("live transport unavailable")

    monkeypatch.setattr(deps.output_service, "publisher", fail_publish)
    await checkpoint(deps, messages)
    assert messages[0].metadata == {"seq": 0} and deps.recorder.boundary == 0
    messages.append(ModelResponse([TextPart("done")]))
    await checkpoint(deps, messages, done=True)
    async with runner_database.sessions.begin() as db:
        saved = decode_state((await db.get(SessionRow, deps.session_id)).runner_state)
        validate_history(saved, await HistoryRepository(db).read_history_entries(deps.session_id))
        assert isinstance(saved, Checkpoint) and saved.next == DoneNext(output="done")


async def test_real_broadcast_cancellation_preserves_committed_checkpoint(
    runner_database, monkeypatch
):
    deps = runner_database.deps()
    messages = [ModelRequest([UserPromptPart("input")])]

    @asynccontextmanager
    async def publisher(*args):
        async def cancelled(event):
            raise asyncio.CancelledError

        yield cancelled

    monkeypatch.setattr(deps.output_service, "publisher", publisher)
    with pytest.raises(asyncio.CancelledError):
        await checkpoint(deps, messages)
    assert deps.recorder.boundary == 0 and messages[0].metadata == {"seq": 0}
    async with runner_database.sessions.begin() as db:
        saved = decode_state((await db.get(SessionRow, deps.session_id)).runner_state)
        assert isinstance(saved, Checkpoint)
        validate_history(saved, await HistoryRepository(db).read_history_entries(deps.session_id))


async def test_full_native_identity_is_required_to_save(runner_database):
    deps = runner_database.deps()
    deps.activity_run_id = "different-native-run"
    with pytest.raises(OwnershipLost):
        await checkpoint(deps, [ModelRequest([UserPromptPart("input")])])
    async with runner_database.sessions.begin() as db:
        assert (await db.get(SessionRow, deps.session_id)).runner_state is None


async def test_history_boundary_is_read_after_conditional_guard(runner_database):
    deps = runner_database.deps()
    attempted = asyncio.Event()
    engine = runner_database.sessions.kw["bind"]
    task = None

    def before_execute(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE sessions"):
            attempted.set()

    try:
        async with runner_database.sessions.begin() as db:
            await SessionRepository(db).guard(
                deps.session_id, deps.activity_id, deps.activity_run_id, (LifecycleStatus.READY,)
            )
            await HistoryRepository(db).append_history(
                deps.session_id, [history(deps.session_id, 2)]
            )
            event.listen(engine.sync_engine, "before_cursor_execute", before_execute)
            task = asyncio.create_task(checkpoint(deps, [ModelRequest([UserPromptPart("input")])]))
            await asyncio.wait_for(attempted.wait(), 2)
            assert not task.done()
        with pytest.raises(ValueError, match="boundary changed"):
            await asyncio.wait_for(task, 2)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", before_execute)
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with runner_database.sessions.begin() as db:
        assert [
            e.seq for e in await HistoryRepository(db).read_history_entries(deps.session_id)
        ] == [2]
        assert (await db.get(SessionRow, deps.session_id)).runner_state is None


async def test_repository_filters_at_one_prewrite_maximum_and_preserves_old_gaps(runner_database):
    session_id = runner_database.session_id
    initial = [history(session_id, 0), history(session_id, 10)]
    async with runner_database.sessions.begin() as db:
        assert await HistoryRepository(db).append_history(session_id, initial) == tuple(initial)
    batch = [
        history(session_id, 5, "old gap"),
        history(session_id, 10, "changed"),
        history(session_id, 15),
        history(session_id, 12),
        history(session_id, 16),
    ]
    async with runner_database.sessions.begin() as db:
        assert await HistoryRepository(db).append_history(session_id, batch) == tuple(batch[2:])
    async with runner_database.sessions.begin() as db:
        stored = await HistoryRepository(db).read_history_entries(session_id)
        assert [e.seq for e in stored] == [0, 10, 12, 15, 16]
        assert stored[:2] == tuple(initial)
        assert await HistoryRepository(db).max_seq(session_id) == 16
        assert (
            await HistoryRepository(db).append_history(
                session_id, [history(session_id, 14, "still sealed")]
            )
            == ()
        )


@pytest.mark.parametrize("invalid", [True, -1, "1", None, 1.5, "duplicate", "session"])
async def test_repository_rejects_invalid_batches_without_writes(runner_database, invalid):
    session_id = runner_database.session_id
    first = history(session_id, 0)
    other = replace(
        first, seq=0 if invalid == "duplicate" else 1 if invalid == "session" else invalid
    )
    if invalid == "session":
        other = replace(other, session_id=uuid4())
    with pytest.raises(ValueError):
        async with runner_database.sessions.begin() as db:
            await HistoryRepository(db).append_history(session_id, [first, other])
    async with runner_database.sessions.begin() as db:
        assert await HistoryRepository(db).read_history_entries(session_id) == ()


async def test_repository_borrows_transaction_and_does_not_commit(runner_database):
    session_id = runner_database.session_id
    with pytest.raises(RuntimeError, match="rollback"):
        async with runner_database.sessions.begin() as db:
            entry = history(session_id, 7)
            assert await HistoryRepository(db).append_history(session_id, [entry]) == (entry,)
            raise RuntimeError("rollback")
    async with runner_database.sessions.begin() as db:
        assert await HistoryRepository(db).read_history_entries(session_id) == ()

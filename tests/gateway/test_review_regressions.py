import asyncio
from typing import Any, cast
from uuid import UUID, uuid4

import psycopg
import pytest

from kapy.agent import AgentResourceLimit
from kapy.gateway.auth import Principal
from kapy.gateway.machines import Connection
from kapy.gateway.params import Create
from kapy.gateway.telegram import TelegramFailure, TelegramFrontend, project
from kapy.rpc import RpcError

from .conftest import MODEL_CONFIG, MODEL_ID
from .test_control import OPERATOR, create
from .test_machines import Peer
from .test_skills import Files, archive
from .test_telegram import Bot, update

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_rejected_running_update_stays_rejected_after_run_completes(gateway, monkeypatch):
    entered, finish = asyncio.Event(), asyncio.Event()
    original = gateway.http_client.send

    async def blocked(*args, **kwargs):
        entered.set()
        await finish.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(gateway.http_client, "send", blocked)
    created = await create(gateway, input="running")
    await asyncio.wait_for(entered.wait(), 1)
    sid = created["session"]["id"]
    change = {
        "session_id": sid,
        "request_id": str(uuid4()),
        "title": "must stay rejected",
        "machine_ids": ["one"],
        "default_machine_id": "one",
        "config": MODEL_CONFIG,
    }
    with pytest.raises(RpcError) as first:
        await gateway.call("session.update", change, principal=OPERATOR)
    assert first.value.code == -32009
    finish.set()
    await gateway.sessions.wait_submission(
        UUID(sid), UUID(created["submission"]["request_id"]), wait_seconds=5
    )
    await gateway.recover()
    assert (await gateway.sessions.get_session(UUID(sid))).title == "test"
    with pytest.raises(RpcError) as replay:
        await gateway.call("session.update", change, principal=OPERATOR)
    assert (replay.value.code, replay.value.message) == (first.value.code, first.value.message)
    assert (await gateway.metadata.request(UUID(change["request_id"])))["error"]["code"] == -32009


async def test_creation_recovers_fixed_snapshot_without_reading_changed_catalog(
    gateway, monkeypatch
):
    finish = gateway.metadata.finish

    async def crash(*args, **kwargs):
        raise OSError("crash after State commit")

    monkeypatch.setattr(gateway.metadata, "finish", crash)
    request_id = str(uuid4())
    with pytest.raises(OSError):
        await create(
            gateway, request_id=request_id, config={"instructions": "original"}, input="resume"
        )
    monkeypatch.setattr(gateway.metadata, "finish", finish)

    async def changed_catalog(*args, **kwargs):
        raise AgentResourceLimit("new catalog too large")

    monkeypatch.setattr(gateway.skills, "catalog", changed_catalog)
    await gateway.recover()
    receipt = await gateway.metadata.request(UUID(request_id))
    assert receipt["result"] is not None and receipt["error"] is None
    assert (
        receipt["operation"]["initial_state"]["data"]["instructions"]
        == "original\nAvailable skill descriptions:\n[]"
    )


async def test_permanent_initial_state_error_does_not_stop_other_recovery(gateway, monkeypatch):
    from kapy.agent import Runner

    initial = Runner.initial_state

    def reject_large(*, instructions, skills):
        if instructions == "oversized":
            raise AgentResourceLimit()
        return initial(instructions=instructions, skills=skills)

    monkeypatch.setattr(Runner, "initial_state", staticmethod(reject_large))
    ids = []
    for instructions in ("oversized", "small"):
        request_id = uuid4()
        ids.append(request_id)
        params = Create(
            request_id=request_id, config={**MODEL_CONFIG, "instructions": instructions}
        )
        await gateway.metadata.reserve(
            request_id, OPERATOR, "session.create", params.model_dump(mode="json"), None
        )
    await gateway.recover()
    assert (await gateway.metadata.request(ids[0]))["error"]["code"] == -32020
    assert (await gateway.metadata.request(ids[1]))["result"] is not None


async def test_sent_timeout_is_unknown_and_failed_ensure_can_retry(gateway):
    sid = UUID((await create(gateway))["session"]["id"])

    class RecoveringPeer(Peer):
        async def call(self, method, params, *, timeout=60):  # noqa: ASYNC109
            if method == "session.ensure" and not self.calls:
                self.calls.append((method, params))
                raise RpcError(-32030, "temporarily unavailable")
            if method == "process.start":
                self.calls.append((method, params))
                await asyncio.Event().wait()
            return await super().call(method, params, timeout=timeout)

    peer = RecoveringPeer()
    connection = Connection("one", cast(Any, peer))
    await gateway.machines.register(connection)
    with pytest.raises(RpcError):
        await gateway.machines.ensure_task(connection, sid)
    assert await gateway.machines.call("one", "process.list", {"session_id": str(sid)}) == {
        "ok": True,
        "released": True,
    }
    with pytest.raises(RpcError) as timeout:
        await gateway.machines.call("one", "process.start", {"session_id": str(sid)}, timeout=0.05)
    assert isinstance(timeout.value.data, dict)
    assert timeout.value.data["unknown"] is True and timeout.value.data["retryable"] is False
    assert len([method for method, _ in peer.calls if method == "process.start"]) == 1


async def test_offline_removed_machine_is_retained_until_safe_release_or_delete(gateway):
    sid = UUID((await create(gateway))["session"]["id"])
    old = Connection("one", cast(Any, Peer()))
    await gateway.machines.register(old)
    await gateway.machines.ensure_task(old, sid)
    await gateway.machines.unregister(old)

    async def change(machines):
        await gateway.call(
            "session.update",
            {
                "session_id": str(sid),
                "request_id": str(uuid4()),
                "title": "test",
                "machine_ids": machines,
                "default_machine_id": machines[0],
                "config": MODEL_CONFIG,
            },
            principal=OPERATOR,
        )

    await change(["two"])
    await gateway.cleanup_once()
    assert (await gateway.metadata.rows("SELECT * FROM gateway_machine_resources"))[0][
        "machine_id"
    ] == "one"
    await change(["one", "two"])
    reconnected = Connection("one", cast(Any, Peer()))
    await gateway.machines.register(reconnected)
    await gateway.machines.ensure_task(reconnected, sid)
    await gateway.cleanup_once()
    assert not any(method == "session.release" for method, _ in cast(Any, reconnected.peer).calls)
    await gateway.machines.unregister(reconnected)
    await change(["two"])
    await gateway.call(
        "session.delete", {"session_id": str(sid), "request_id": str(uuid4())}, principal=OPERATOR
    )
    row = (await gateway.metadata.rows("SELECT * FROM gateway_session_cleanup"))[0]
    assert set(row["pending_machine_ids"]) == {"one", "two"}
    released = Connection("one", cast(Any, Peer()))
    await gateway.machines.register(released)
    await gateway.cleanup_once()
    assert [method for method, _ in cast(Any, released.peer).calls] == ["session.release"]
    assert (await gateway.metadata.rows("SELECT * FROM gateway_session_cleanup"))[0][
        "pending_machine_ids"
    ] == ["two"]


async def test_pending_skill_transfer_keeps_original_default_machine(gateway, monkeypatch):
    sid = (await create(gateway, machine_ids=["one", "two"]))["session"]["id"]
    files = Files(archive())
    machines = []

    async def disconnected(machine, method, params, *, timeout=60):  # noqa: ASYNC109
        machines.append(machine)
        raise RpcError(-32022, "offline")

    monkeypatch.setattr(gateway.machines, "call", disconnected)
    params = {"session_id": sid, "archive_path": "/source", "request_id": str(uuid4())}
    with pytest.raises(RpcError):
        await gateway.call("skill.create", params, principal=OPERATOR)
    await gateway.call(
        "session.update",
        {
            "session_id": sid,
            "request_id": str(uuid4()),
            "title": "test",
            "machine_ids": ["one", "two"],
            "default_machine_id": "two",
            "config": MODEL_CONFIG,
        },
        principal=OPERATOR,
    )

    async def working(machine, method, params, *, timeout=60):  # noqa: ASYNC109
        machines.append(machine)
        return await files.call(machine, method, params, timeout=timeout)

    monkeypatch.setattr(gateway.machines, "call", working)
    await gateway.call("skill.create", params, principal=OPERATOR)
    assert set(machines) == {"one"}


async def test_model_command_updates_waiting_session_with_persistent_run_id(gateway):
    bot = Bot(gateway)
    await bot.ingest([update(1, "/machine one"), update(2, "finish a run")])
    await bot.process_once()
    await bot.process_once()
    sid = (await bot.route(-100, 0))["session_id"]
    from kapy.gateway.telegram import request_id

    await gateway.sessions.wait_submission(
        sid, UUID(request_id(12345, 2, "message.create")), wait_seconds=5
    )
    assert (await gateway.sessions.get_session(sid)).run_id is not None
    await bot.ingest([update(3, f'/model {{"model_id":"{MODEL_ID}","max_output_tokens":1000}}')])
    await bot.process_once()
    assert (await gateway.sessions.get_session(sid)).config["model"]["max_output_tokens"] == 1000


async def test_terminal_projection_releases_accumulated_messages():
    preview, projection = project(
        [{"kind": "text_delta", "message_id": "same", "data": {"text": "Hello"}}], {}
    )
    assert preview == "Hello"
    _, projection = project([{"kind": "final", "data": {"output": "Hello"}}], projection)
    assert projection["pending"]["text"] == "Hello"
    assert projection["pending"]["next"] == {"version": 1, "messages": {}}


@pytest.mark.parametrize("loop", ["poll", "process", "deliver"])
async def test_telegram_loops_survive_one_database_outage(gateway, monkeypatch, loop):
    bot = Bot(gateway)
    attempts = 0
    original_sleep = asyncio.sleep

    async def quick_sleep(delay):
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", quick_sleep)

    async def operation(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise psycopg.OperationalError("temporary")
        bot.disabled = True
        return []

    if loop == "poll":
        monkeypatch.setattr(bot.metadata, "rows", operation)

        async def api(*args, **kwargs):
            return []

        monkeypatch.setattr(bot, "api", api)
    else:
        monkeypatch.setattr(bot, loop + "_once", operation)
    await asyncio.wait_for(getattr(bot, loop)(), 1)
    assert attempts == 2


async def test_chat_send_serializes_waiters_and_honors_new_retry_after(gateway, monkeypatch):
    bot = Bot(gateway)
    clock = [0.0]
    calls = []
    original_sleep = asyncio.sleep

    async def sleep(delay):
        clock[0] += delay
        await original_sleep(0)

    monkeypatch.setattr("kapy.gateway.telegram.time.monotonic", lambda: clock[0])
    monkeypatch.setattr(asyncio, "sleep", sleep)

    async def api(method, params):
        calls.append(clock[0])
        await original_sleep(0)
        if len(calls) == 1:
            raise TelegramFailure(429, 45)
        return {}

    monkeypatch.setattr(bot, "api", api)
    results = await asyncio.gather(
        *[TelegramFrontend.send(bot, -100, index, "text") for index in range(3)],
        return_exceptions=True,
    )
    assert isinstance(results[0], TelegramFailure)
    assert calls == [0, 45, 46]


async def test_supervisor_observes_unexpected_failure_and_preserves_cancellation(
    monkeypatch, caplog
):
    from kapy.gateway.app import supervise

    entered = asyncio.Event()
    attempts = 0
    original_sleep = asyncio.sleep

    async def quick_sleep(delay):
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", quick_sleep)

    async def worker():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("not a transient storage error")
        entered.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(supervise("test-worker", worker))
    await asyncio.wait_for(entered.wait(), 1)
    assert "restarting" in caplog.text and attempts == 2
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_online_removal_preserves_daemon_session_until_final_delete(gateway):
    sid = UUID((await create(gateway))["session"]["id"])
    peer = Peer()
    connection = Connection("one", cast(Any, peer))
    await gateway.machines.register(connection)
    await gateway.machines.ensure_task(connection, sid)
    for machines in (["two"], ["one"]):
        await gateway.call(
            "session.update",
            {
                "session_id": str(sid),
                "request_id": str(uuid4()),
                "title": "test",
                "machine_ids": machines,
                "default_machine_id": machines[0],
                "config": MODEL_CONFIG,
            },
            principal=OPERATOR,
        )
        await gateway.cleanup_once()
        assert not any(method == "session.release" for method, _ in peer.calls)
    assert await gateway.machines.call("one", "process.list", {"session_id": str(sid)}) == {
        "ok": True,
        "released": True,
    }
    await gateway.call(
        "session.delete", {"session_id": str(sid), "request_id": str(uuid4())}, principal=OPERATOR
    )
    await gateway.cleanup_once()
    assert [method for method, _ in peer.calls].count("session.release") == 1


async def test_machine_capacity_error_does_not_seal_committed_skill_recovery(gateway, monkeypatch):
    sid = (await create(gateway))["session"]["id"]
    principal = Principal("session", "one", UUID(sid))
    files = Files(archive())
    monkeypatch.setattr(gateway.machines, "call", files.call)
    finish = gateway.metadata.finish

    async def crash(*args, **kwargs):
        raise OSError("Gateway lost before persisting creator and result")

    monkeypatch.setattr(gateway.metadata, "finish", crash)
    request_id = uuid4()
    params = {"session_id": sid, "request_id": str(request_id), "archive_path": "/skill.zip"}
    with pytest.raises(OSError):
        await gateway.call("skill.create", params, principal=principal)
    assert len(await gateway.skills.catalog()) == 1
    monkeypatch.setattr(gateway.metadata, "finish", finish)

    async def full(machine, method, params, *, timeout=60):  # noqa: ASYNC109
        if method == "file.abort":
            return {"aborted": False}
        raise RpcError(-32020, "RPC queue or transfer capacity is full")

    monkeypatch.setattr(gateway.machines, "call", full)
    with pytest.raises(RpcError) as error:
        await gateway.call("skill.create", params, principal=principal)
    assert error.value.code == -32020
    pending = await gateway.metadata.request(request_id)
    assert pending["error"] is None and pending["result"] is None
    monkeypatch.setattr(gateway.machines, "call", files.call)
    result = await gateway.call("skill.create", params, principal=principal)
    assert len(await gateway.skills.catalog()) == 1
    assert (await gateway.metadata.request(request_id))["result"] == result
    assert (await gateway.metadata.rows("SELECT * FROM gateway_skill_access"))[0][
        "creator_principal"
    ] == principal.id

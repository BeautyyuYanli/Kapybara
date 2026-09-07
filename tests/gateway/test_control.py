import asyncio
from uuid import UUID, uuid4

import pytest

from kapy.gateway.auth import Principal
from kapy.rpc import RpcError

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
OPERATOR = Principal("operator")


async def create(gateway, principal=OPERATOR, **extra):
    return await gateway.call(
        "session.create",
        {
            "request_id": str(uuid4()),
            "title": "test",
            "machine_ids": ["one"],
            "default_machine_id": "one",
            "config": {},
            **extra,
        },
        principal=principal,
    )


async def test_receipt_idempotency_owner_and_deleted_wait(gateway):
    request_id = str(uuid4())
    params = {"request_id": request_id, "machine_ids": ["one"], "input": "hello"}
    created = await gateway.call("session.create", params, principal=OPERATOR)
    assert await gateway.call("session.create", params, principal=OPERATOR) == created
    sid = created["session"]["id"]
    status = await gateway.call(
        "session.wait",
        {
            "session_id": sid,
            "request_id": request_id,
            "wait_seconds": 5,
        },
        principal=OPERATOR,
    )
    assert status["completion"]["output"] == "hello"
    assert status["completion"]["outcome"] == "completed"
    await gateway.call(
        "session.delete", {"session_id": sid, "request_id": str(uuid4())}, principal=OPERATOR
    )
    assert (
        await gateway.call(
            "session.wait",
            {
                "session_id": sid,
                "request_id": request_id,
            },
            principal=OPERATOR,
        )
        == status
    )
    stranger = Principal("telegram", telegram_route=(12345, -100, 7))
    with pytest.raises(RpcError) as error:
        await gateway.call("session.create", params, principal=stranger)
    assert error.value.code == -32001
    with pytest.raises(RpcError) as error:
        await gateway.call("session.create", {**params, "input": "changed"}, principal=OPERATOR)
    assert error.value.code == -32009


async def test_self_direct_child_machine_subset_and_channels(gateway):
    parent = (await create(gateway))["session"]["id"]
    identity = Principal("session", "one", UUID(parent))
    child = (await create(gateway, identity))["session"]["id"]
    sibling = (await create(gateway))["session"]["id"]
    assert (await gateway.call("session.get", {"session_id": child}, principal=identity))[
        "id"
    ] == child
    with pytest.raises(RpcError):
        await gateway.call("session.get", {"session_id": sibling}, principal=identity)
    with pytest.raises(RpcError):
        await create(gateway, identity, machine_ids=["two"], default_machine_id="two")
    visible = await gateway.call("session.list", {}, principal=identity)
    assert {entry["id"] for entry in visible["items"]} == {parent, child}
    channel = uuid4()
    with pytest.raises(PermissionError):
        await gateway.authorize_wait(UUID(parent), (channel,))
    receipt = await gateway.call(
        "session.input",
        {
            "session_id": child,
            "request_id": str(uuid4()),
            "payload": "nested",
        },
        principal=identity,
    )
    await gateway.authorize_wait(UUID(parent), (UUID(receipt["waiting_id"]),))
    with pytest.raises(PermissionError):
        await gateway.authorize_wait(UUID(sibling), (UUID(receipt["waiting_id"]),))


async def test_recover_create_after_state_commit_before_access(gateway, monkeypatch):
    original = gateway.metadata.finish

    async def fail(*args, **kwargs):
        raise OSError("crash before metadata commit")

    monkeypatch.setattr(gateway.metadata, "finish", fail)
    request_id = str(uuid4())
    with pytest.raises(OSError):
        await create(gateway, request_id=request_id, input="recover")
    assert not (await gateway.metadata.rows("SELECT * FROM gateway_session_access"))
    monkeypatch.setattr(gateway.metadata, "finish", original)
    await gateway.recover()
    rows = await gateway.metadata.rows("SELECT * FROM gateway_session_access")
    assert len(rows) == 1
    status = await gateway.call(
        "session.wait",
        {
            "session_id": str(rows[0]["session_id"]),
            "request_id": request_id,
            "wait_seconds": 5,
        },
        principal=OPERATOR,
    )
    assert status["completion"]["output"] == "recover"


async def test_delete_outbox_cleans_payload_before_offline_machine(gateway):
    sid = UUID((await create(gateway))["session"]["id"])
    ref = await gateway.payload_store.put(sid, b"durable-media")
    assert await gateway.payload_store.get(sid, ref) == b"durable-media"
    await gateway.call(
        "session.delete", {"session_id": str(sid), "request_id": str(uuid4())}, principal=OPERATOR
    )
    await gateway.cleanup_once()
    rows = await gateway.metadata.rows("SELECT * FROM gateway_session_cleanup")
    assert rows[0]["payload_pending"] is False
    assert rows[0]["pending_machine_ids"] == ["one"]
    assert rows[0]["state"] == "releasing"


async def test_history_export_pagination_and_parallel_waiters(gateway):
    created = await create(gateway, input="visible")
    sid = created["session"]["id"]
    waits = await asyncio.gather(
        *[
            gateway.call(
                "session.wait",
                {
                    "session_id": sid,
                    "request_id": created["submission"]["request_id"],
                    "wait_seconds": 5,
                },
                principal=OPERATOR,
            )
            for _ in range(5)
        ]
    )
    assert all(item == waits[0] for item in waits)
    page = await gateway.call("history.export", {"session_id": sid, "limit": 1}, principal=OPERATOR)
    count = len(page["items"])
    while page["has_more"]:
        page = await gateway.call(
            "history.export",
            {
                "session_id": sid,
                "after": page["next_cursor"],
                "snapshot": page["snapshot_cursor"],
                "limit": 1,
            },
            principal=OPERATOR,
        )
        count += len(page["items"])
    assert count > 1

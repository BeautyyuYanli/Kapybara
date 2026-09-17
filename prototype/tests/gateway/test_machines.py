import asyncio
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from kapy.gateway.machines import Connection
from kapy.rpc import RpcError

from .test_control import create

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class Peer:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.ensure = asyncio.Event()

    async def call(self, method, params, *, timeout=60):  # noqa: ASYNC109
        self.calls.append((method, params))
        if method == "session.ensure":
            self.ensure.set()
            return {"session_id": params["session_id"], "cwd": "/test/session"}
        return {"ok": True, "released": True}

    async def aclose(self):
        self.closed = True


async def test_proactive_ensure_fencing_and_bound_session_capability(gateway):
    sid = UUID((await create(gateway))["session"]["id"])
    peer = Peer()
    old = Connection("one", cast(Any, peer))
    await gateway.machines.register(old)
    await asyncio.wait_for(peer.ensure.wait(), 1)
    ensured = peer.calls[0][1]
    assert gateway.machines.auth.session(sid, "one", ensured["session_token"]).session_id == sid
    with pytest.raises(RpcError):
        gateway.machines.auth.session(sid, "two", ensured["session_token"])
    newer = Connection("one", cast(Any, Peer()))
    await gateway.machines.register(newer)
    assert peer.closed
    await gateway.machines.unregister(old)
    assert gateway.machines.connections["one"] is newer
    with pytest.raises(RpcError):
        await gateway.machines.proxy(old, "control.proxy", {}, gateway)
    result = await gateway.machines.call(
        "one",
        "process.wait",
        {
            "session_id": str(sid),
            "process_id": str(uuid4()),
            "wait_ms": 0,
        },
    )
    assert result == {"ok": True, "released": True}
    assert cast(Any, newer.peer).calls[0][0] == "session.ensure"


async def test_proxy_caller_target_separation_and_no_implicit_admin(gateway):
    sid = UUID((await create(gateway))["session"]["id"])
    unrelated = (await create(gateway))["session"]["id"]
    connection = Connection("one", cast(Any, Peer()))
    await gateway.machines.register(connection)
    token = gateway.machines.auth.token(sid, "one")
    envelope = {
        "auth": {"kind": "session", "session_id": str(sid), "token": token},
        "method": "session.get",
        "params": {"session_id": unrelated},
    }
    with pytest.raises(RpcError) as error:
        await gateway.machines.proxy(connection, "control.proxy", envelope, gateway)
    assert error.value.code == -32001
    for auth in ({"kind": "user"}, {"kind": "user", "token": ""}, {}):
        with pytest.raises(RpcError):
            await gateway.machines.proxy(
                connection, "control.proxy", {**envelope, "auth": auth}, gateway
            )
    result = await gateway.machines.proxy(
        connection,
        "control.proxy",
        {
            **envelope,
            "auth": {"kind": "user", "token": "admin-test"},
        },
        gateway,
    )
    assert result["id"] == unrelated


async def test_offline_deadline_is_bounded(gateway):
    sid = (await create(gateway))["session"]["id"]
    started = asyncio.get_running_loop().time()
    with pytest.raises(RpcError) as error:
        await gateway.machines.call("one", "process.list", {"session_id": sid}, timeout=0.05)
    assert error.value.code == -32022
    assert asyncio.get_running_loop().time() - started < 0.5

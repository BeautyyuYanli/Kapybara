import asyncio
import json

import httpx
import pytest

from kapy.gateway import create_app

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_authenticated_shared_codec_and_transport_limits(gateway):
    app = create_app(gateway.settings, frontends=[])
    app.state.control = gateway
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        request = {"jsonrpc": "2.0", "id": 1, "method": "session.list", "params": {}}
        assert (await client.post("/rpc", json=request)).status_code == 401
        headers = {"Authorization": "Bearer admin-test"}
        result = await client.post("/rpc", json=request, headers=headers)
        assert result.status_code == 200
        assert result.json() == {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"items": [], "next_after": None},
        }
        del request["id"]
        assert (await client.post("/rpc", json=request, headers=headers)).status_code == 204
        result = await client.post("/rpc", content=b"\xff", headers=headers)
        assert result.json()["error"]["code"] == -32700
        result = await client.post("/rpc", content=b"x" * 1_048_577, headers=headers)
        assert result.status_code == 413
        result = await client.post(
            "/rpc",
            content=json.dumps(
                [
                    {**request, "id": 2},
                    {**request, "id": 3, "params": {"unknown": True}},
                ]
            ),
            headers=headers,
        )
        assert len(result.json()) == 2
        assert result.json()[1]["error"]["code"] == -32602


async def test_websocket_rejects_missing_or_wrong_machine_token_before_upgrade(gateway):
    app = create_app(gateway.settings, frontends=[])
    app.state.control = gateway
    app.state.machines = gateway.machines

    def scope(headers):
        return {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": "/rpc/machines/one",
            "raw_path": b"/rpc/machines/one",
            "query_string": b"",
            "root_path": "",
            "headers": headers,
            "subprotocols": ["kapy.jsonrpc.v1"],
            "client": ("test", 1234),
            "server": ("test", 80),
        }

    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})
    serving = asyncio.create_task(
        app(scope([(b"authorization", b"Bearer machine-one")]), incoming.get, outgoing.put)
    )
    try:
        async with asyncio.timeout(5):
            accepted = await outgoing.get()
            assert accepted["type"] == "websocket.accept"
            async with gateway.machines._changed:
                await gateway.machines._changed.wait_for(
                    lambda: "one" in gateway.machines.connections
                )
            original = gateway.machines.connections["one"]
            for headers in ([], [(b"authorization", b"Bearer machine-two")]):
                rejected = asyncio.Queue()
                messages = asyncio.Queue()
                await messages.put({"type": "websocket.connect"})
                await app(scope(headers), messages.get, rejected.put)
                response = rejected.get_nowait()
                assert response["type"] == "websocket.close" and response["code"] == 1008
                assert gateway.machines.connections["one"] is original

            # Both denied upgrades leave the authenticated connection able to serve RPC.
            await incoming.put(
                {
                    "type": "websocket.receive",
                    "text": json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": "still-connected",
                            "method": "control.proxy",
                            "params": {
                                "auth": {"kind": "user", "token": "admin-test"},
                                "method": "session.list",
                                "params": {},
                            },
                        }
                    ),
                }
            )
            reply = await outgoing.get()
            assert json.loads(reply["text"]) == {
                "jsonrpc": "2.0",
                "id": "still-connected",
                "result": {"items": [], "next_after": None},
            }
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(serving, 5)

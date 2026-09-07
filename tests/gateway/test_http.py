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

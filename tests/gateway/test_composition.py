import json
from uuid import uuid4

import httpx2
import psycopg
import pytest
from psycopg import sql
from valkey.asyncio import Valkey

from kapy.gateway import create_app
from kapy.gateway.auth import Principal
from kapy.settings import Settings

from .conftest import DATABASE, VALKEY

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_real_lifespan_runner_checkpoint_and_resource_shutdown(monkeypatch):
    original_client = httpx2.AsyncClient
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        chunks = [
            {
                "id": "response",
                "object": "chat.completion.chunk",
                "model": "gpt-5.6-luna",
                "created": 1,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "actual runner"},
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "response",
                "object": "chat.completion.chunk",
                "model": "gpt-5.6-luna",
                "created": 1,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 123, "completion_tokens": 4, "total_tokens": 127},
            },
        ]
        body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    class Client(original_client):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx2.MockTransport(respond))

    monkeypatch.setattr(httpx2, "AsyncClient", Client)
    schema = "gw_app_" + uuid4().hex
    namespace = "gw_app:" + uuid4().hex
    settings = Settings(
        database_schema=schema,
        valkey_namespace=namespace,
        control_token="test-admin",
        session_signing_key="test-signing",
        openai_api_key="test-provider",
        context_window_tokens=100_000,
    )
    app = create_app(settings, frontends=[])
    try:
        async with app.router.lifespan_context(app):
            pool = app.state.metadata.pool
            request_id = str(uuid4())
            created = await app.state.control.call(
                "session.create",
                {
                    "request_id": request_id,
                    "input": "hello",
                },
                principal=Principal("operator"),
            )
            result = await app.state.control.call(
                "session.wait",
                {
                    "session_id": created["session"]["id"],
                    "request_id": request_id,
                    "wait_seconds": 5,
                },
                principal=Principal("operator"),
            )
            assert result["completion"]["output"] == "actual runner"
            assert len(requests) == 1
            output = await app.state.control.call(
                "session.output",
                {
                    "session_id": created["session"]["id"],
                },
                principal=Principal("operator"),
            )
            assert any(item["kind"] == "model_response" for item in output["items"])
            assert not pool.closed
        assert pool.closed
    finally:
        async with await psycopg.AsyncConnection.connect(DATABASE) as conn:
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        async with Valkey.from_url(VALKEY) as client:
            keys = [key async for key in client.scan_iter(match=namespace + "*")]
            if keys:
                await client.delete(*keys)

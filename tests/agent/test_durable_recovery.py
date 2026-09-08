import asyncio
import base64
import json
import os
from dataclasses import asdict
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from psycopg import sql
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart

from kapy.agent import (
    AgentPayloadStore,
    OpenAICompatibleBackend,
    PayloadCorrupt,
    PayloadNotFound,
    PayloadRef,
    Runner,
    RunnerConfig,
)
from kapy.agent.codec import INLINE_LIMIT, MessageCodec, json_bytes
from kapy.state import RunnerState

from .test_runner import Caller, Context, authorize, response  # type: ignore[missing-import]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_media_and_external_context_survive_full_resource_restart() -> None:
    schema = f"test_agent_restart_{uuid4().hex}"
    dsn = os.environ.get("KAPY_DATABASE_URL", "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy")
    config = RunnerConfig(1_000_000)
    original_bytes = b"\x89PNG\r\n\x1a\noriginal durable image"
    caller = Caller(original_bytes)
    calls = 0

    def first_provider(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(name="read_media", args={"path": "original.png"}, call_id="media-call")
        raise asyncio.CancelledError

    try:
        async with (
            AsyncConnectionPool(dsn, open=False) as pool,
            httpx2.AsyncClient(transport=httpx2.MockTransport(first_provider)) as client,
        ):
            payloads = AgentPayloadStore(pool, schema=schema)
            await payloads.initialize()
            agent = Runner(
                config,
                caller,
                model_backend=OpenAICompatibleBackend(
                    base_url="https://model.invalid/v1",
                    api_key=SecretStr("dummy-key"),
                    http_client=client,
                ),
                payload_store=payloads,
                authorize_wait=authorize,
            )  # type: ignore[bad-argument-type]
            ctx = Context(agent.initial_state(instructions="durable-instructions", skills=[]))
            codec = MessageCodec(payloads, ctx.session.id)
            data = await codec.load(ctx.state)
            # Bounded historical messages make the whole context exceed the inline ceiling.
            for index in range(13):
                prompt = f"archived-input-{index}:" + "x" * 90_000
                data["cycles"].append(
                    {
                        "turn_id": str(uuid4()),
                        "closed": True,
                        "level": 0,
                        "inputs": [prompt],
                        "input_ids": [str(uuid4())],
                        "outputs": ["prior reply"],
                        "messages": [
                            await codec.encode(ModelRequest.user_text_prompt(prompt)),
                            await codec.encode(ModelResponse([TextPart("prior reply")])),
                        ],
                    }
                )
            assert len(json_bytes(data)) > INLINE_LIMIT
            ctx.state = await codec.state(data)
            assert "payload" in ctx.state.data
            with pytest.raises(asyncio.CancelledError):
                await agent(ctx)
            assert calls == 2
            assert sum(method == "file.pull" for _, method, _, _ in caller.calls) == 1
            saved = json.loads(json.dumps(asdict(ctx.state)))
            number, session, run_id = ctx.checkpoint_number, ctx.session, ctx.run_id
            restored_data = await codec.load(RunnerState(**saved))
            refs = [
                ref["ref"]
                for cycle in restored_data["cycles"]
                for message in cycle["messages"]
                for part in message["parts"]
                for ref in (part.get("metadata") or {}).get("kapy_media_refs", [])
            ]
            assert len(refs) == 1
            media_ref = PayloadRef(**refs[0])
            other_session = uuid4()
            other_ref = await payloads.put(other_session, b"session B actual bytes")
            assert "payload" in saved["data"]
        # The original pool, HTTP client, store and Runner are all discarded here.
        del agent, payloads, codec, pool, client
        resumed_caller = Caller(b"machine path now contains unrelated bytes")
        observed = []

        def resumed_provider(request: httpx2.Request) -> httpx2.Response:
            body = json.loads(request.content)
            observed.append(body)
            assert "durable-instructions" in json.dumps(body["messages"])
            assert "archived-input-0:" in json.dumps(body["messages"])
            images = [
                part["image_url"]["url"]
                for message in body["messages"]
                if isinstance(message.get("content"), list)
                for part in message["content"]
                if part.get("type") == "image_url"
            ]
            assert len(images) == 1
            assert base64.b64decode(images[0].split(",", 1)[1]) == original_bytes
            return response(text="Recovered the original bytes")

        def recovered_context() -> Context:
            recovered = Context(RunnerState(**json.loads(json.dumps(saved))))
            recovered.session, recovered.run_id = session, run_id
            recovered.checkpoint_number = number
            recovered.inputs = ()
            recovered.attempt, recovered.recovered = 2, True
            return recovered

        async with (
            AsyncConnectionPool(dsn, open=False) as new_pool,
            httpx2.AsyncClient(transport=httpx2.MockTransport(resumed_provider)) as new_client,
        ):
            new_store = AgentPayloadStore(new_pool, schema=schema)
            await new_store.initialize()

            def new_runner() -> Runner:
                return Runner(
                    config,
                    resumed_caller,
                    model_backend=OpenAICompatibleBackend(
                        base_url="https://model.invalid/v1",
                        api_key=SecretStr("dummy-key"),
                        http_client=new_client,
                    ),
                    payload_store=new_store,
                    authorize_wait=authorize,
                )

            result = await new_runner()(recovered_context())  # type: ignore[bad-argument-type]
            assert result.output == "Recovered the original bytes" and len(observed) == 1
            for failure in ("missing", "corrupt"):
                async with new_pool.connection() as conn:
                    if failure == "missing":
                        await conn.execute(
                            sql.SQL("DELETE FROM {} WHERE session_id=%s AND sha256=%s").format(
                                new_store.table
                            ),
                            (session.id, media_ref.sha256),
                        )
                    else:
                        await conn.execute(
                            sql.SQL(
                                "UPDATE {} SET data=%s WHERE session_id=%s AND sha256=%s"
                            ).format(new_store.table),
                            (b"corrupted storage", session.id, media_ref.sha256),
                        )
                expected: Any = PayloadNotFound if failure == "missing" else PayloadCorrupt
                with pytest.raises(expected):
                    await new_runner()(recovered_context())  # type: ignore[bad-argument-type]
                if failure == "missing":
                    assert await new_store.put(session.id, original_bytes) == media_ref
            assert len(observed) == 1 and resumed_caller.calls == []
            await new_store.delete_session(session.id)
            assert await new_store.get(other_session, other_ref) == b"session B actual bytes"
            with pytest.raises(PayloadNotFound):
                await new_store.get(session.id, PayloadRef(**saved["data"]["payload"]))
    finally:
        async with AsyncConnectionPool(dsn, open=False) as cleanup_pool:
            async with cleanup_pool.connection() as conn:
                await conn.execute(
                    sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
                )

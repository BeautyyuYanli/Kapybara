"""Public control HTTP contracts exercise real services and PostgreSQL storage."""

import asyncio
import socket
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
import uvicorn
import websockets
from fastapi import Depends, FastAPI, HTTPException
from pydantic import TypeAdapter
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, UserPromptPart
from starlette.requests import HTTPConnection
from websockets.exceptions import ConnectionClosedError, InvalidStatus

from kapy.tmpv2.agent_output import AgentOutputService
from kapy.tmpv2.agent_runner import (
    MessageCommitted,
    OutputEvent,
    SessionBusy,
    TextDelta,
)
from kapy.tmpv2.agent_runner.repository import AgentRepository
from kapy.tmpv2.control.models import ModelService
from kapy.tmpv2.control.sessions import SessionService
from kapy.tmpv2.http import create_router

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def application(database, sessions, *, dependencies=()):
    app = FastAPI()
    app.include_router(
        create_router(ModelService(database.sessions), sessions, agent=Agent("test")),
        dependencies=list(dependencies),
    )
    return app


async def test_http_create_input_background_and_withdrawal(database, seed_session, monkeypatch):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    await seed_session(session_id)
    existing = await sessions.get_session(session_id)
    starts = []

    async def start(session_id, **kwargs):
        # Background execution sees a committed queue, independent of the HTTP transaction.
        starts.append((session_id, await sessions.read_inputs(session_id, "queued"), kwargs))
        raise SessionBusy("another request won")

    monkeypatch.setattr(sessions, "start_runner", start)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(database, sessions)), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/sessions",
            json={
                "provider_id": str(existing.provider_id),
                "model_name": existing.model_name,
                "input": {"content": "first"},
            },
        )
        assert response.status_code == 201
        created_id = response.json()["id"]
        assert str(starts[0][0]) == created_id and starts[0][1][0].content == "first"
        assert starts[0][2]["realtime_output"] is True
        assert starts[0][2]["output_flush_interval"] == 0.5
        response = await client.post(f"/api/sessions/{created_id}/inputs", json={"content": "next"})
        assert response.status_code == 202
        input_id = response.json()["id"]
        pending = (await client.get(f"/api/sessions/{created_id}/inputs")).json()
        assert [item["content"] for item in pending] == ["first", "next"]
        assert (await client.delete(f"/api/sessions/{created_id}/inputs/{input_id}")).json() is True
        assert (
            await client.delete(f"/api/sessions/{created_id}/inputs/{input_id}")
        ).json() is False
        token = uuid4()
        async with database.sessions.begin() as db:
            await AgentRepository(db).acquire(starts[0][0], token, heartbeat_timeout=60)
        response = await client.post(f"/api/sessions/{created_id}/inputs", json={"content": "busy"})
        assert response.status_code == 202 and len(starts) == 2
        assert (await client.get(f"/api/sessions/{created_id}/runner")).json() is True
        cancelled = await client.post(f"/api/sessions/{created_id}/cancel")
        assert cancelled.status_code == 202 and cancelled.content == b""
        assert (await client.get(f"/api/sessions/{created_id}/cancel")).json() is True
        empty = await client.post(
            "/api/sessions",
            json={
                "provider_id": str(existing.provider_id),
                "model_name": existing.model_name,
            },
        )
        assert empty.status_code == 201 and len(starts) == 2


async def test_http_history_pages_and_canonical_model_paths(database, seed_history, seed_session):
    session_id = await seed_history(
        [ModelRequest(parts=[UserPromptPart(str(i))]) for i in range(5)]
    )
    await seed_session(session_id)
    sessions = SessionService(database.sessions)
    existing = await sessions.get_session(session_id)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application(database, sessions)), base_url="http://test"
    ) as client:
        first = (await client.get(f"/api/sessions/{session_id}/history?limit=2")).json()
        assert [item["seq"] for item in first["items"]] == [3, 4] and first["has_more"]
        older = (
            await client.get(f"/api/sessions/{session_id}/history?before_seq=3&limit=2")
        ).json()
        assert [item["seq"] for item in older["items"]] == [1, 2] and older["has_more"]
        oldest = (
            await client.get(f"/api/sessions/{session_id}/history?before_seq=1&limit=2")
        ).json()
        assert [item["seq"] for item in oldest["items"]] == [0] and not oldest["has_more"]
        await client.post(
            "/api/models",
            json={
                "provider_id": str(existing.provider_id),
                "model_name": "vendor/model",
                "context_window": 1000,
            },
        )
        model_path = f"/api/models/{existing.provider_id}/vendor/model"
        assert (await client.get(model_path)).json()["model_name"] == "vendor/model"
        assert (await client.patch(model_path, json={"name": "renamed"})).json()[
            "name"
        ] == "renamed"
        models = (await client.get("/api/models?limit=1")).json()
        assert len(models["items"]) == 1 and models["has_more"]
        providers = (await client.get("/api/providers")).json()
        assert providers["items"][0]["id"] == str(existing.provider_id)
        assert "test-key" not in str(providers)
        assert (await client.delete(model_path)).status_code == 204
        assert (await client.get(model_path)).status_code == 404
        assert (await client.delete(f"/api/providers/{existing.provider_id}")).status_code == 409
        page = (await client.get("/api/sessions?limit=1")).json()
        assert page["items"][0]["id"] == str(session_id) and not page["has_more"]
        missing = uuid4()
        assert (await client.get(f"/api/sessions/{missing}/history")).json() == {
            "items": [],
            "has_more": False,
        }
        assert (await client.get(f"/api/sessions/{missing}/runner")).json() is False
        assert (await client.get(f"/api/sessions/{missing}/cancel")).json() is False
        for path in (
            "/api/models?limit=201",
            "/api/providers?offset=-1",
            f"/api/sessions/{session_id}/history?before_seq=-1",
        ):
            assert (await client.get(path)).status_code == 422


async def test_validation_omits_raw_credentials_and_internal_errors(database, monkeypatch):
    sessions = SessionService(database.sessions)
    app = application(database, sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/providers",
            json={
                "api_key": "never-echo-this-key",
                "provider_kwargs": {"secret": "another-key"},
            },
        )
        assert response.status_code == 422
        assert "never-echo" not in response.text and "another-key" not in response.text
        assert all(set(item) == {"loc", "msg", "type"} for item in response.json()["detail"])

        async def fail(session_id):
            raise RuntimeError("internal-secret")

        monkeypatch.setattr(sessions, "get_session", fail)
        response = await client.get(f"/api/sessions/{uuid4()}")
        assert response.status_code == 500 and "internal-secret" not in response.text


@asynccontextmanager
async def serve(app):
    # Use a real ASGI server so WebSocket disconnect and generator ownership are exercised.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(3):
                while not server.started:
                    if task.done():
                        task.result()
                    await asyncio.sleep(0.01)
            yield f"ws://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            async with asyncio.timeout(3):
                await task


async def test_http_accepts_input_before_background_runner_finishes(
    database, seed_session, monkeypatch
):
    sessions = SessionService(database.sessions)
    session_id = uuid4()
    await seed_session(session_id)
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def start(session_id, **kwargs):
        started.set()
        try:
            await release.wait()
        finally:
            finished.set()

    monkeypatch.setattr(sessions, "start_runner", start)
    async with serve(application(database, sessions)) as base:
        try:
            async with httpx.AsyncClient(base_url=base.replace("ws://", "http://", 1)) as client:
                async with asyncio.timeout(3):
                    response = await client.post(
                        f"/api/sessions/{session_id}/inputs", json={"content": "run later"}
                    )
                    await started.wait()
                assert response.status_code == 202
                assert not finished.is_set()
        finally:
            release.set()
            if started.is_set():
                async with asyncio.timeout(3):
                    await finished.wait()


async def test_websocket_replay_live_frames_and_idle_subscription_cleanup(
    database, valkey_client, seed_history
):
    session_id = await seed_history([ModelRequest(parts=[UserPromptPart("history")])])
    outputs = AgentOutputService(valkey_client)
    sessions = SessionService(database.sessions, output_service=outputs)
    app = application(database, sessions)
    adapter = TypeAdapter(OutputEvent)
    async with serve(app) as base:
        async with websockets.connect(f"{base}/api/sessions/{session_id}/live?after_seq=-1") as ws:
            first = adapter.validate_json(await ws.recv())
            assert isinstance(first, MessageCommitted) and first.message.seq == 0
            delta = TextDelta(session_id, 1, 0, "text", "replace", "live")
            async with outputs.publisher(session_id, flush_interval=0) as publish:
                await publish(delta)
            assert adapter.validate_json(await ws.recv()) == delta
        async with asyncio.timeout(2):
            # Valkey has no notification API for another connection unsubscribing.
            while (await valkey_client.pubsub_numsub(f"kapy:agent-output:{session_id}"))[0][1]:  # noqa: ASYNC110
                await asyncio.sleep(0.01)
        async with websockets.connect(f"{base}/api/sessions/{session_id}/live?after_seq=0") as ws:
            await ws.send("unsupported")
            with pytest.raises(ConnectionClosedError) as closed:
                await ws.recv()
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == 1003
        with pytest.raises(InvalidStatus):
            async with websockets.connect(f"{base}/api/sessions/{session_id}/live"):
                pass


async def test_websocket_failure_and_host_authentication(database):
    sessions = SessionService(database.sessions)  # Iteration cannot subscribe without transport.

    async def authenticate(connection: HTTPConnection):
        if connection.query_params.get("authorized") != "yes":
            raise HTTPException(status_code=403)

    app = application(database, sessions, dependencies=[Depends(authenticate)])
    async with serve(app) as base:
        path = f"{base}/api/sessions/{uuid4()}/live?after_seq=-1"
        with pytest.raises(InvalidStatus):
            async with websockets.connect(path):
                pass
        async with websockets.connect(path + "&authorized=yes") as ws:
            with pytest.raises(ConnectionClosedError) as closed:
                await ws.recv()
            assert closed.value.rcvd is not None
            assert closed.value.rcvd.code == 1011

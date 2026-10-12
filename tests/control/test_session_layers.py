"""Layer configuration stays public/fixed; transactional State stays internal."""

from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from kapy.control.models import ModelService
from kapy.control.sessions import CreateSession, SessionService
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.control.types import JsonObject
from kapy.interfaces.http import create_router
from kapy.layers import make_layers
from kapy.layers.examples import echo
from kapy.layers.registry import LAYERS, get_layers
from kapy.lifecycle import LifecycleError, LifecycleStatus

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_http_layer_validation_defaults_and_immutable_configuration(database):
    service = SessionService(database.sessions)
    app = FastAPI()
    app.include_router(create_router(ModelService(database.sessions), service))
    base = {"provider_id": str(uuid4()), "model_name": "test"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for invalid in (
            {"layers": {"unknown": {}}},
            {"layers": {"shell": {}}},
            {"layers": {"shell": {"cwd": "relative"}}},
            {
                "layers": {"shell": {"cwd": "/tmp"}},
                "plugins": [{"provider": "builtin", "name": "shell", "config": {}}],
            },
            {"layer_states": {"shell": {"jobs": {"injected": 1}}}},
        ):
            response = await client.post("/api/sessions", json=base | invalid)
            assert response.status_code == 422
        assert not (await service.list_sessions()).items
        response = await client.post(
            "/api/sessions", json=base | {"layers": {"shell": {"cwd": "/tmp"}}}
        )
        assert response.status_code == 201
        created = response.json()
        assert created["layers"]["shell"]["cwd"] == "/tmp"
        assert created["layers"]["shell"]["env"] == {}
        assert "layer_states" not in created
        session_id = UUID(created["id"])
        async with database.sessions.begin() as db:
            row = await db.get(SessionRow, session_id)
            assert row is not None and row.layer_states == {"shell": {"jobs": {}}}
        for replacement in ({"layers": {}}, {"layer_states": {}}):
            response = await client.patch(f"/api/sessions/{session_id}", json=replacement)
            assert response.status_code == 422
        assert (await client.get(f"/api/sessions/{session_id}")).json() == created


async def test_atomic_layer_message_save_cleanup_and_retry_identity(database, monkeypatch):
    monkeypatch.setitem(LAYERS, "echo", make_layers({"echo": echo.make}, get_layers)["echo"])
    service = SessionService(database.sessions)
    session = await service.create_session(
        CreateSession(
            provider_id=uuid4(),
            model_name="test",
            layers={"shell": {"cwd": "/tmp"}, "echo": {}},
        )
    )
    async with database.sessions.begin() as db:
        repo = SessionRepository(db)
        assert await repo.read_runner_state(session.id) == (None, 0)
    with pytest.raises(LifecycleError):
        async with database.sessions.begin() as db:
            await SessionRepository(db).save_layer_cleanup(session.id, "shell", {"jobs": {}})
    final: dict[str, JsonObject] = {"shell": {"jobs": {"known": 8}}, "echo": {"calls": 2}}
    with pytest.raises(RuntimeError, match="rollback"):
        async with database.sessions.begin() as db:
            await SessionRepository(db).save_runner_state(
                session.id, expected_version=0, runner_state="messages", layer_states=final
            )
            raise RuntimeError("rollback")
    async with database.sessions.begin() as db:
        repo = SessionRepository(db)
        row = await db.get(SessionRow, session.id)
        assert row is not None and row.layer_states == {
            "shell": {"jobs": {}},
            "echo": {"calls": 0},
        }
        assert await repo.read_runner_state(session.id) == (None, 0)
        await repo.save_runner_state(
            session.id, expected_version=0, runner_state="messages", layer_states=final
        )
        saved_at = (await repo.get_session(session.id)).updated_at
    async with database.sessions.begin() as db:
        repo = SessionRepository(db)
        await repo.save_runner_state(
            session.id, expected_version=0, runner_state="messages", layer_states=final
        )
        assert (await repo.get_session(session.id)).updated_at == saved_at
        assert await repo.read_runner_state(session.id) == ("messages", 1)
    changed = dict(final)
    changed["echo"] = {"calls": 3}
    for invalid in ({}, changed):
        with pytest.raises(ValueError):
            async with database.sessions.begin() as db:
                await SessionRepository(db).save_runner_state(
                    session.id, expected_version=0, runner_state="messages", layer_states=invalid
                )
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session.id)
        assert row is not None and row.layer_states == final
        row.status = LifecycleStatus.CLOSING
    async with database.sessions.begin() as db:
        repo = SessionRepository(db)
        await repo.save_layer_cleanup(session.id, "shell", {"jobs": {}})
        row = await db.get(SessionRow, session.id)
        assert row is not None and row.layer_states == {
            "shell": {"jobs": {}},
            "echo": {"calls": 2},
        }
        assert await repo.read_runner_state(session.id) == ("messages", 1)

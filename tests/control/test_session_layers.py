"""Layer configuration stays public/fixed; transactional State stays internal."""

from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from kapy.control.models import ModelService
from kapy.control.sessions.models import SessionRow
from kapy.interfaces.http import create_router

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def test_http_layer_validation_defaults_and_immutable_configuration(
    database, session_runtime
):
    service = session_runtime.service(database.sessions)
    app = FastAPI()
    app.include_router(create_router(ModelService(database.sessions), service))
    base = {"activity_id": str(uuid4()), "provider_id": str(uuid4()), "model_name": "test"}
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
            response = await client.post(
                "/api/sessions", json=base | invalid | {"activity_id": str(uuid4())}
            )
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
            assert row is not None and row.layer_states == {
                "shell": {"scope_id": str(session_id), "jobs": {}}
            }
        for replacement in ({"layers": {}}, {"layer_states": {}}):
            response = await client.patch(f"/api/sessions/{session_id}", json=replacement)
            assert response.status_code == 422
        assert (await client.get(f"/api/sessions/{session_id}")).json() == created

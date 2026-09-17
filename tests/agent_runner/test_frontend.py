"""SPA mounting and provider readback use real FastAPI and service boundaries."""

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai import Agent

from kapy.control.models import ModelService
from kapy.control.sessions import SessionService
from kapy.plugins.http import create_frontend_router, create_router


def test_missing_frontend_build_fails_setup(tmp_path):
    with pytest.raises(RuntimeError, match="entry"):
        create_frontend_router(tmp_path)


@pytest.mark.asyncio
async def test_native_spa_fallback_preserves_api_assets_and_lifespan(tmp_path):
    from contextlib import asynccontextmanager

    events = []

    @asynccontextmanager
    async def lifespan(app):
        events.append("start")
        yield
        events.append("stop")

    (tmp_path / "index.html").write_text("<main>SPA</main>")
    (tmp_path / "main.js").write_text("export {}")
    app = FastAPI(lifespan=lifespan)

    @app.get("/api/check")
    def check():
        return {"ok": True}

    app.include_router(create_frontend_router(tmp_path))
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://local"
        ) as client:
            for path in ("/app/", "/app/providers/123", "/app/unknown"):
                result = await client.get(path, headers={"accept": "text/html"})
                assert result.status_code == 200 and "SPA" in result.text
            for path in ("/app/missing.js", "/app/assets/missing.css", "/api/missing"):
                assert (await client.get(path)).status_code == 404
            assert (await client.get("/app/main.js")).status_code == 200
            assert (await client.get("/api/check")).json() == {"ok": True}
            assert (await client.get("/openapi.json")).json()["openapi"]
            assert events == ["start"]
    assert events == ["start", "stop"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_provider_kwargs_round_trip_and_patch_keeps_key_write_only(database):
    app = FastAPI()
    app.include_router(
        create_router(
            ModelService(database.sessions), SessionService(database.sessions), agent=Agent("test")
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://local"
    ) as client:
        kwargs = {"retry_options": {"attempts": 3}}
        response = await client.post(
            "/api/providers",
            json={
                "name": "SPA provider",
                "api_key": "never-echo-test-key",
                "provider_class": "pydantic_ai.providers.google:GoogleProvider",
                "model_class": "pydantic_ai.models.google:GoogleModel",
                "provider_kwargs": kwargs,
            },
        )
        assert response.status_code == 201
        record = response.json()
        assert record["provider_kwargs"] == kwargs and "api_key" not in record
        path = f"/api/providers/{record['id']}"
        assert (await client.get(path)).json()["provider_kwargs"] == kwargs
        assert (await client.get("/api/providers")).json()["items"][0]["provider_kwargs"] == kwargs
        assert (await client.patch(path, json={"name": "renamed"})).json()[
            "provider_kwargs"
        ] == kwargs
        cleared = await client.patch(path, json={"provider_kwargs": {}, "base_url": None})
        assert cleared.json()["provider_kwargs"] == {}
        assert "never-echo-test-key" not in cleared.text
        assert "api_key" not in cleared.json()

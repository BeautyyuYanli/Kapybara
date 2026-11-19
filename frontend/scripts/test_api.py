"""Local-only deterministic browser host with an isolated disposable PostgreSQL schema.

Run from the repository: .venv/bin/uvicorn test_api:app --app-dir frontend/scripts
--host 127.0.0.1 --port 8001. Build frontend first. Configuration CRUD uses the
same local PostgreSQL server as tests/conftest.py, with its own disposable schema.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi import FastAPI
from psycopg import sql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kapy.agent_history.models import history_metadata
from kapy.control.database import ControlTable
from kapy.control.models import ModelService
from kapy.control.sessions import SessionService
from kapy.interfaces.http import create_frontend_router, create_router

schema = "spa_test_" + uuid4().hex
url = os.environ.get("KAPY_DATABASE_URL", "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy")
engine = create_async_engine(
    url.replace("postgresql://", "postgresql+psycopg://"),
    connect_args={"options": f"-csearch_path={schema},pg_catalog"},
)


@asynccontextmanager
async def lifespan(app):
    async with await psycopg.AsyncConnection.connect(url, autocommit=True) as db:
        await db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        async with engine.begin() as db:
            await db.run_sync(ControlTable.metadata.create_all)
            await db.run_sync(history_metadata.create_all)
        yield
    finally:
        await engine.dispose()
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as db:
            await db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


factory = async_sessionmaker(engine, expire_on_commit=False)
app = FastAPI(lifespan=lifespan)
app.include_router(create_router(ModelService(factory), SessionService(factory)))
app.include_router(create_frontend_router(Path(__file__).resolve().parents[1] / "dist"))

"""Local-only deterministic browser host with an isolated disposable PostgreSQL schema.

Run from the repository: .venv/bin/uvicorn test_api:app --app-dir frontend/scripts
--host 127.0.0.1 --port 8001. Build frontend first. Uses the same local test database
as tests/conftest.py; configure Temporal with KAPY_TEMPORAL_ADDRESS and Valkey with
KAPY_VALKEY_URL. Its embedded Worker owns create/close scope calls on a unique
queue; it never registers run or makes model requests. The Worker/client contexts
exit before the disposable schema is dropped. No normal application rows change;
this host never reads .env.
"""

import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import psycopg
from fastapi import FastAPI
from psycopg import sql
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker
from valkey.asyncio import Valkey

from kapy.agent_history.models import history_metadata
from kapy.agent_output import AgentOutputService
from kapy.control.database import ControlTable
from kapy.control.models import ModelService
from kapy.control.sessions import SessionService
from kapy.interfaces.http import create_frontend_router, create_router
from kapy.runner import RunnerActivity

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
        client = await Client.connect(
            os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
            data_converter=pydantic_data_converter,
        )
        queue = f"spa-test-{uuid4()}"
        async with Valkey.from_url(
            os.environ.get("KAPY_VALKEY_URL", "redis://127.0.0.1:56379/0")
        ) as valkey:
            outputs = AgentOutputService(valkey, channel_prefix=f"spa-test-{uuid4()}")
            runner = RunnerActivity(factory, outputs)
            sessions = SessionService(
                factory, output_service=outputs, temporal_client=client, temporal_task_queue=queue
            )
            app.include_router(create_router(ModelService(factory), sessions))
            async with Worker(client, task_queue=queue, activities=[runner.create, runner.close]):
                yield
    finally:
        await engine.dispose()
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as db:
            await db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


factory = async_sessionmaker(engine, expire_on_commit=False)
app = FastAPI(lifespan=lifespan)
app.include_router(create_frontend_router(Path(__file__).resolve().parents[1] / "dist"))

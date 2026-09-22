"""Real PostgreSQL tests own a random schema and dispose every engine they create."""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from psycopg import sql
from pydantic_ai.toolsets import FunctionToolset
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from valkey.asyncio import Valkey

from kapy.agent_runner.models import agent_metadata
from kapy.agent_runner.repository import AgentRepository
from kapy.agent_runner.types import ContextPageRecord, NextStep
from kapy.control.database import ControlTable
from kapy.control.sessions import models as session_models  # noqa: F401
from kapy.session_lease import open_session_lease
from kapy.session_lease import service as lease_service
from kapy.session_lease.models import lease_metadata

DATABASE_URL = os.environ.get(
    "KAPY_DATABASE_URL", "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy"
)


@pytest_asyncio.fixture
async def valkey_client():
    client = Valkey.from_url(
        os.environ.get("KAPY_VALKEY_URL", "valkey://127.0.0.1:56379/0"),
        socket_connect_timeout=1,
    )
    try:
        await client.ping()
        yield client
    finally:
        await client.aclose()


@dataclass
class Database:
    schema: str
    url: str
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


def make_engine(schema: str) -> AsyncEngine:
    return create_async_engine(
        DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1),
        connect_args={"options": f"-csearch_path={schema},pg_catalog"},
    )


@pytest_asyncio.fixture
async def database() -> AsyncIterator[Database]:
    schema = "tmpv2_agent_test_" + uuid4().hex
    async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as db:
        await db.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    engine = make_engine(schema)
    try:
        async with engine.begin() as db:
            await db.run_sync(agent_metadata.create_all)
            await db.run_sync(lease_metadata.create_all)
            await db.run_sync(ControlTable.metadata.create_all)
        yield Database(
            schema, DATABASE_URL, engine, async_sessionmaker(engine, expire_on_commit=False)
        )
    finally:
        await engine.dispose()
        async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as db:
            await db.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def seed_history(database):
    async def seed(messages, next_step: NextStep = "done", *, compaction_seq=None):
        session_id = uuid4()
        async with (
            open_session_lease(session_id, session_factory=database.sessions) as lease,
            database.sessions.begin() as db,
        ):
            await lease.lock_owned(db)
            repo = AgentRepository(db)
            await repo.resume(session_id)
            await repo.save_checkpoint(
                session_id, next_step=next_step, start_seq=0, messages=messages
            )
            if compaction_seq is not None:
                await repo.save_page(
                    session_id,
                    ContextPageRecord(compaction_seq, "summary/v1", {"summary": "saved summary"}),
                )
        return session_id

    return seed


@pytest.fixture
def toolset_lifecycle():
    class ObservedToolset(FunctionToolset[None]):
        def __init__(self):
            super().__init__()
            self.events = []

        async def __aenter__(self):
            result = await super().__aenter__()
            self.events.append("enter")
            return result

        async def __aexit__(self, *args):
            try:
                return await super().__aexit__(*args)
            finally:
                self.events.append("exit")

    return ObservedToolset()


@pytest.fixture
def heartbeat_observation(monkeypatch):
    tasks = set()
    called = asyncio.Event()
    original = lease_service._update_owned

    async def observe(db, lease, *, release):
        task = asyncio.current_task()
        assert task is not None
        await original(db, lease, release=release)
        if not release:
            tasks.add(task)
            called.set()

    monkeypatch.setattr(lease_service, "_update_owned", observe)
    return tasks, called


@pytest.fixture
def seed_session(database):
    """Give user-facing service tests a business row; bare runner tests need none."""
    from kapy.control.models.models import ModelRow, ProviderRow
    from kapy.control.sessions.models import SessionRow

    async def seed(session_id):
        async with database.sessions.begin() as db:
            provider = ProviderRow(
                name="test",
                api_key="test-key",
                provider_class="pydantic_ai.providers.openai:OpenAIProvider",
                model_class="pydantic_ai.models.openai:OpenAIChatModel",
            )
            db.add(provider)
            db.add(
                ModelRow(
                    provider_id=provider.id, model_name="test", name="test", context_window=10**9
                )
            )
            db.add(SessionRow(id=session_id, provider_id=provider.id, model_name="test"))

    return seed


@pytest.fixture
def session_model(monkeypatch):
    """Replace only SDK model construction for existing runner-behavior scenarios.

    Catalog integration tests separately exercise the configured SDK constructor
    and real protocol requests. These tests retain their FunctionModel/TestModel.
    """
    from pydantic_ai.models import infer_model

    from kapy.control.sessions import service

    def use(model):
        model = infer_model(model)
        monkeypatch.setattr(service, "build_model", lambda *args, **kwargs: model)

    return use


@pytest.fixture
def wait_for_lock(database):
    """Wait for an independently identified backend to block on a PostgreSQL lock."""

    async def wait(pid):
        async with asyncio.timeout(5):
            while True:
                async with database.sessions.begin() as db:
                    waiting = (
                        await db.execute(
                            text(
                                "SELECT wait_event_type = 'Lock' "
                                "FROM pg_stat_activity WHERE pid=:pid"
                            ),
                            {"pid": pid},
                        )
                    ).scalar_one()
                if waiting:
                    return
                await asyncio.sleep(0.01)

    return wait

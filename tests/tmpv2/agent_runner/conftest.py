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
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from kapy.tmpv2.agent_runner.models import agent_metadata
from kapy.tmpv2.agent_runner.repository import AgentRepository
from kapy.tmpv2.agent_runner.types import NextStep
from kapy.tmpv2.control.database import ControlTable
from kapy.tmpv2.control.sessions import models as session_models  # noqa: F401

DATABASE_URL = os.environ.get(
    "KAPY_DATABASE_URL", "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy"
)


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
        session_id, token = uuid4(), uuid4()
        async with database.sessions.begin() as db:
            repo = AgentRepository(db)
            await repo.acquire(session_id, token, heartbeat_timeout=60)
            await repo.lock_owned(session_id, token)
            await repo.save_checkpoint(
                session_id, next_step=next_step, start_seq=0, messages=messages
            )
            if compaction_seq is not None:
                await repo.save_compaction(
                    session_id, last_message_seq=compaction_seq, text="saved summary"
                )
            await repo.release(session_id, token)
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
    original = AgentRepository.heartbeat

    async def observe(self, session_id, lock_token):
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        await original(self, session_id, lock_token)
        called.set()

    monkeypatch.setattr(AgentRepository, "heartbeat", observe)
    return tasks, called

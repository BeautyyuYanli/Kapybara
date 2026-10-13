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
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from temporalio.client import Client
from valkey.asyncio import Valkey

from kapy.agent_history import HistoryMessage, HistoryRepository
from kapy.agent_history.models import history_metadata
from kapy.agent_output import AgentOutputService
from kapy.control.database import ControlTable
from kapy.control.sessions import models as session_models  # noqa: F401
from kapy.control.sessions.operations import SessionOperationRow

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
            await db.run_sync(history_metadata.create_all)
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
    """Write complete test history in an independent fixture transaction, without a runner."""
    from kapy.control.sessions.models import SessionRow

    async def seed(messages):
        session_id = uuid4()
        async with database.sessions.begin() as db:
            db.add(SessionRow(id=session_id, provider_id=uuid4(), model_name="test"))
            await db.flush()
            await HistoryRepository(db).append_history(
                session_id,
                [HistoryMessage(session_id, seq, message) for seq, message in enumerate(messages)],
            )
        return session_id

    return seed


@pytest.fixture
def seed_session(database):
    """Create catalog configuration and a session; history fixtures may already own its row."""
    from kapy.control.models.models import ModelRow, ProviderRow
    from kapy.control.sessions.models import SessionRow

    async def seed(session_id, *, running=False):
        async with database.sessions.begin() as db:
            existing = await db.get(SessionRow, session_id)
            provider = ProviderRow(
                id=existing.provider_id if existing is not None else uuid4(),
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
            if existing is None:
                existing = SessionRow(id=session_id, provider_id=provider.id, model_name="test")
                db.add(existing)
            if running:
                from datetime import timedelta

                from kapy.control.sessions.types import OperationKind, OperationPhase
                from kapy.control.types import utc_now
                from kapy.lifecycle import LifecycleStatus

                existing.status = LifecycleStatus.RUNNING
                existing.current_activity_id = session_id
                db.add(
                    SessionOperationRow(
                        activity_id=session_id,
                        session_id=session_id,
                        kind=OperationKind.RUN,
                        phase=OperationPhase.RUNNING,
                        request={},
                        deadline=utc_now() + timedelta(hours=1),
                    )
                )

    return seed


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


@dataclass
class SessionRuntime:
    client: Client
    queue: str
    outputs: AgentOutputService

    def service(self, factory):
        from kapy.control.sessions import SessionService

        return SessionService(
            factory,
            output_service=self.outputs,
            temporal_client=self.client,
            temporal_task_queue=self.queue,
        )


@pytest_asyncio.fixture
async def session_runtime(database, valkey_client):
    from temporalio.client import Client
    from temporalio.contrib.pydantic import pydantic_data_converter
    from temporalio.worker import Worker

    from kapy.agent_output import AgentOutputService
    from kapy.runner import RunnerActivity

    client = await Client.connect(
        os.environ.get("KAPY_TEMPORAL_ADDRESS", "localhost:7233"),
        data_converter=pydantic_data_converter,
    )
    queue = f"session-test-{uuid4()}"
    outputs = AgentOutputService(valkey_client, channel_prefix=f"session-test-{uuid4()}")
    runner = RunnerActivity(database.sessions, outputs)
    async with Worker(
        client, task_queue=queue, activities=[runner.create, runner.run, runner.close]
    ):
        yield SessionRuntime(client, queue, outputs)

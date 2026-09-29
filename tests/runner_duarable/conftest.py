"""Temporal state tests own a migrated PostgreSQL schema and existing session."""

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import psycopg
import pytest_asyncio
from psycopg import sql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from valkey.asyncio import Valkey

from kapy.agent_output import AgentOutputService
from kapy.application.resources import open_core_database
from kapy.application.settings import CommonSettings
from kapy.control.sessions.models import SessionRow
from kapy.database.schema import migrate
from kapy.runner_duarable.context import bind_output_service


@dataclass
class RunnerDatabase:
    settings: CommonSettings
    sessions: async_sessionmaker[AsyncSession]
    session_id: UUID
    outputs: AgentOutputService


@pytest_asyncio.fixture
async def runner_database() -> AsyncIterator[RunnerDatabase]:
    settings = CommonSettings.model_validate(
        {**os.environ, "KAPY_DATABASE_SCHEMA": f"runner_state_test_{uuid4().hex}"}
    )
    try:
        await migrate(settings, "upgrade")
        async with open_core_database(settings) as engine:
            sessions = async_sessionmaker(engine, expire_on_commit=False)
            session_id = uuid4()
            async with sessions.begin() as db:
                db.add(SessionRow(id=session_id, provider_id=uuid4(), model_name="test"))
            async with Valkey.from_url(settings.valkey_url.get_secret_value()) as client:
                outputs = AgentOutputService(client, channel_prefix=f"runner-test:{uuid4()}")
                with bind_output_service(outputs):
                    yield RunnerDatabase(settings, sessions, session_id, outputs)
    finally:
        async with await psycopg.AsyncConnection.connect(
            settings.database_url.get_secret_value(), autocommit=True
        ) as db:
            await db.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    sql.Identifier(settings.database_schema)
                )
            )

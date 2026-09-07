"""Each test owns a fresh schema; no shared service is restarted or flushed."""

import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest_asyncio
from psycopg import sql
from psycopg.rows import dict_row
from valkey.asyncio import Valkey

from kapy.state import RunnerState, SessionRunner, SessionService, SessionSpec, migrate

DATABASE_URL = os.environ.get(
    "KAPY_DATABASE_URL", "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy"
)
VALKEY_URL = os.environ.get("KAPY_VALKEY_URL", "redis://127.0.0.1:56379/0")


def spec(title: str = "test") -> SessionSpec:
    return SessionSpec(
        title, ("machine-a", "machine-b"), "machine-a", {}, RunnerState("fake-v1", {})
    )


@dataclass
class Database:
    schema: str
    services: list[SessionService] = field(default_factory=list)

    async def start(self, runner: SessionRunner, *, valkey_url: str = VALKEY_URL) -> SessionService:
        service = SessionService(
            database_url=DATABASE_URL,
            valkey_url=valkey_url,
            runner=runner,
            schema=self.schema,
            namespace=self.schema,
        )
        await service.__aenter__()
        self.services.append(service)
        return service

    async def rows(self, statement: str, params: Any = None) -> list[dict[str, Any]]:
        async with await psycopg.AsyncConnection[dict[str, Any]].connect(
            DATABASE_URL, row_factory=dict_row
        ) as conn:
            await conn.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(self.schema)
                )
            )
            return await (await conn.execute(statement.encode(), params)).fetchall()

    async def completed(self, request_id: UUID, *, wait_seconds: float = 15) -> dict[str, Any]:
        async with asyncio.timeout(wait_seconds):
            while True:
                rows = await self.rows("SELECT completion FROM requests WHERE id=%s", (request_id,))
                if rows and rows[0]["completion"] is not None:
                    return rows[0]["completion"]
                await asyncio.sleep(0.02)


@pytest_asyncio.fixture
async def database() -> AsyncIterator[Database]:
    schema = "state_test_" + uuid4().hex
    await migrate(DATABASE_URL, schema=schema)
    client = Valkey.from_url(VALKEY_URL)
    assert await client.ping()
    await client.aclose()
    database = Database(schema)
    try:
        yield database
    finally:
        for service in reversed(database.services):
            await service.__aexit__(None, None, None)
        # Only the exact random schema created above belongs to this fixture.
        async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

"""PostgreSQL connection ownership, migrations, and short serialized writes."""

import hashlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Any
from uuid import UUID, uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .contracts import Conflict, InvalidArgument, ServiceUnavailable

type Connection = psycopg.AsyncConnection[dict[str, Any]]


def schema_name(schema: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema) or schema.startswith("pg_"):
        raise InvalidArgument("schema must be a lowercase PostgreSQL identifier outside pg_*")
    if schema in {"public", "information_schema"}:
        raise InvalidArgument("State requires its own schema")
    return schema


def lock_key(schema: str, purpose: str) -> int:
    return int.from_bytes(
        hashlib.sha256(f"kapy:{schema}:{purpose}".encode()).digest()[:8], signed=True
    )


async def migrate(database_url: str, *, schema: str = "kapy_state") -> None:
    schema_name(schema)
    async with await psycopg.AsyncConnection[dict[str, Any]].connect(
        database_url, row_factory=dict_row
    ) as conn:
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (lock_key(schema, "migration"),))
        await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        await conn.execute(
            sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(schema))
        )
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, checksum TEXT NOT NULL)"
        )
        for path in sorted(
            files("kapy.state").joinpath("migrations").iterdir(), key=lambda p: p.name
        ):
            if not path.name.endswith(".sql"):
                continue
            content = path.read_text()
            checksum = hashlib.sha256(content.encode()).hexdigest()
            row = await (
                await conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = %s", (path.name,)
                )
            ).fetchone()
            if row:
                if row["checksum"] != checksum:
                    raise Conflict("an applied migration has changed")
                continue
            await conn.execute(content.encode())
            await conn.execute(
                "INSERT INTO schema_migrations VALUES (%s,%s)", (path.name, checksum)
            )
        await conn.execute(
            "INSERT INTO service_meta(epoch) VALUES (%s) ON CONFLICT DO NOTHING", (uuid4(),)
        )


class Store:
    def __init__(self, database_url: str, schema: str):
        self.database_url = database_url
        self.schema = schema_name(schema)
        self.epoch: UUID | None = None
        self.lease: Connection | None = None
        self.pool = AsyncConnectionPool(
            database_url,
            open=False,
            min_size=1,
            max_size=12,
            kwargs={"row_factory": dict_row, "autocommit": True},
            configure=self._configure,
        )

    async def _configure(self, conn: Connection) -> None:
        await conn.execute(
            sql.SQL("SET search_path TO {}, pg_catalog").format(sql.Identifier(self.schema))
        )

    async def open(self) -> None:
        self.lease = await psycopg.AsyncConnection[dict[str, Any]].connect(
            self.database_url, autocommit=True, row_factory=dict_row
        )
        row = await (
            await self.lease.execute(
                "SELECT pg_try_advisory_lock(%s) AS acquired", (lock_key(self.schema, "control"),)
            )
        ).fetchone()
        if not row or not row["acquired"]:
            await self.close()
            raise Conflict("another control process owns this schema")
        await self.pool.open(wait=True)
        self.epoch = uuid4()
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute("UPDATE service_meta SET epoch = %s", (self.epoch,))

    async def close(self) -> None:
        self.epoch = None
        await self.pool.close()
        if self.lease:
            await self.lease.close()
            self.lease = None

    async def check_lease(self) -> None:
        if not self.lease or self.lease.closed:
            raise ServiceUnavailable("control-process lease is closed")
        await self.lease.execute("SELECT 1")

    @asynccontextmanager
    async def write(self) -> AsyncIterator[Connection]:
        if not self.epoch or not self.lease or self.lease.closed:
            raise ServiceUnavailable("State service is closed")
        async with self.pool.connection() as conn, conn.transaction():
            row = await (await conn.execute("SELECT epoch FROM service_meta FOR UPDATE")).fetchone()
            if not row or row["epoch"] != self.epoch:
                raise ServiceUnavailable("control-process lease has changed")
            yield conn

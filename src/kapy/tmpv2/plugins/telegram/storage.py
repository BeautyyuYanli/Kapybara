"""A single queued SQLite connection serializes independent short transactions.

Explicit BEGIN avoids aiosqlite's legacy transaction behavior for reads and DDL.
The database belongs to one Telegram process, on a persistent local filesystem.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
from sqlalchemy import event
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


@asynccontextmanager
async def open_storage(path: Path, *, create: bool = False) -> AsyncIterator[AsyncEngine]:
    if not path.is_absolute():
        raise ValueError("SQLite path must be absolute")
    if create:
        await anyio.Path(path.parent).mkdir(parents=True, exist_ok=True)
    elif not await anyio.Path(path).is_file():
        raise FileNotFoundError("Telegram database is missing; run telegram db upgrade")
    engine = create_async_engine(
        URL.create("sqlite+aiosqlite", database=str(path)),
        pool_size=1,
        max_overflow=0,
        connect_args={"timeout": 5},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure(connection, _record):
        connection.isolation_level = None
        cursor = connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    @event.listens_for(engine.sync_engine, "begin")
    def begin(connection):
        connection.exec_driver_sql("BEGIN")

    try:
        yield engine
    finally:
        await engine.dispose()

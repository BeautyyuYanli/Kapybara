"""Create resources for the calling process; callers finish tasks before context exit.

No services, interface discovery, global objects or schema changes occur here.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from valkey.asyncio import Valkey

from .settings import CommonSettings


@asynccontextmanager
async def open_core_database(settings: CommonSettings) -> AsyncIterator[AsyncEngine]:
    url = settings.database_url.get_secret_value().replace(
        "postgresql://", "postgresql+psycopg://", 1
    )
    if not url.startswith("postgresql+psycopg://"):
        raise ValueError("Core database requires PostgreSQL with psycopg")
    engine = create_async_engine(
        url,
        connect_args={"options": f"-csearch_path={settings.database_schema},pg_catalog"},
    )
    try:
        yield engine
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class Resources:
    core_session_factory: async_sessionmaker[AsyncSession]
    valkey: Valkey


@asynccontextmanager
async def open_resources(settings: CommonSettings) -> AsyncIterator[Resources]:
    async with open_core_database(settings) as engine:
        client = Valkey.from_url(settings.valkey_url.get_secret_value())
        try:
            yield Resources(async_sessionmaker(engine, expire_on_commit=False), client)
        finally:
            await client.aclose()

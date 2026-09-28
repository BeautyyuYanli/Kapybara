"""Create resources for the calling process; callers finish tasks before context exit.

No services, interface discovery, global objects or schema changes occur here.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from pydantic_ai.durable_exec.temporal import PydanticAIPlugin
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from temporalio.client import Client
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
    temporal_client: Client


async def connect_temporal(settings: CommonSettings) -> Client:
    """Connect once per process using the Agent payload converter and sandbox plugin.

    Temporal's Client has no explicit close API. Owners retain it for their
    lifespan and finish users (including Workers) before releasing references.
    """
    return await Client.connect(
        settings.temporal_address,
        namespace=settings.temporal_namespace,
        plugins=[PydanticAIPlugin()],
    )


@asynccontextmanager
async def open_resources(settings: CommonSettings) -> AsyncIterator[Resources]:
    async with open_core_database(settings) as engine:
        client = Valkey.from_url(settings.valkey_url.get_secret_value())
        try:
            temporal_client = await connect_temporal(settings)
            yield Resources(
                async_sessionmaker(engine, expire_on_commit=False), client, temporal_client
            )
        finally:
            await client.aclose()

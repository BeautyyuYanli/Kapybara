"""Unsupported old state is rejected without converting or clearing persisted data."""

import hashlib
from importlib.resources import files
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

from kapy.gateway.storage import migrate as migrate_gateway
from kapy.state import migrate

from .conftest import DATABASE_URL

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.mark.parametrize("gateway", [False, True])
async def test_old_protocol_state_is_left_untouched(gateway: bool) -> None:
    schema = "old_state_" + uuid4().hex
    identifier = uuid4()
    try:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            await conn.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema))
            )
            if gateway:
                await conn.execute("CREATE TABLE gateway_channel_grants(waiting_id uuid)")
                await conn.execute("INSERT INTO gateway_channel_grants VALUES (%s)", (identifier,))
            else:
                initial = files("kapy.state").joinpath("migrations/001_initial.sql").read_text()
                await conn.execute(initial.encode())
                await conn.execute("CREATE TABLE schema_migrations(version text, checksum text)")
                await conn.execute(
                    "INSERT INTO schema_migrations VALUES ('001_initial.sql', %s)",
                    (hashlib.sha256(initial.encode()).hexdigest(),),
                )
                await conn.execute(
                    "INSERT INTO sessions(id,title,machine_ids,config,initial_state,status) "
                    "VALUES (%s,'old session','[]','{}','{}','waiting')",
                    (identifier,),
                )
        with pytest.raises(psycopg.errors.RaiseException, match="unsupported"):
            await (migrate_gateway if gateway else migrate)(DATABASE_URL, schema=schema)
        async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
            await conn.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema))
            )
            if gateway:
                row = await (
                    await conn.execute("SELECT waiting_id FROM gateway_channel_grants")
                ).fetchone()
            else:
                row = await (await conn.execute("SELECT id FROM sessions")).fetchone()
                table = await (
                    await conn.execute("SELECT to_regclass('waiting_channels')")
                ).fetchone()
                assert table == (None,)
            assert row == (identifier,)
    finally:
        async with await psycopg.AsyncConnection.connect(DATABASE_URL, autocommit=True) as conn:
            await conn.execute(
                sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(sql.Identifier(schema))
            )

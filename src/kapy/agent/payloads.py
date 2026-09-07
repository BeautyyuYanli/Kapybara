"""Immutable session-scoped payloads, borrowing the control application's pool."""

import asyncio
import hashlib
from dataclasses import dataclass
from uuid import UUID

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

MAX_PAYLOAD = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class PayloadRef:
    sha256: str
    bytes: int


class PayloadNotFound(Exception):
    """The referenced payload does not exist within this session."""


class PayloadCorrupt(Exception):
    """Payload bytes do not match their durable reference."""


class PayloadTooLarge(Exception):
    """A durable payload exceeds 64 MiB."""


class AgentPayloadStore:
    def __init__(self, pool: AsyncConnectionPool, *, schema: str = "kapy_agent") -> None:
        self.pool = pool
        self.schema = sql.Identifier(schema)
        self.table = sql.Identifier(schema, "agent_payloads")

    async def initialize(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(self.schema))
            await conn.execute(
                sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                session_id uuid NOT NULL, sha256 text NOT NULL, bytes bigint NOT NULL,
                data bytea NOT NULL, PRIMARY KEY(session_id,sha256))""").format(self.table)
            )

    async def put(self, session_id: UUID, data: bytes) -> PayloadRef:
        if len(data) > MAX_PAYLOAD:
            raise PayloadTooLarge("Payload exceeds 64 MiB")
        ref = PayloadRef(hashlib.sha256(data).hexdigest(), len(data))

        async def write() -> None:
            async with self.pool.connection() as conn:
                await conn.execute(
                    sql.SQL("""INSERT INTO {} (session_id,sha256,bytes,data)
                    VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING""").format(self.table),
                    (session_id, ref.sha256, ref.bytes, data),
                )

        task = asyncio.create_task(write())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # State's cancellation barrier must also cover payload writes before cleanup.
            try:
                await task
            finally:
                raise
        return ref

    async def get(self, session_id: UUID, ref: PayloadRef) -> bytes:
        async with self.pool.connection() as conn:
            cursor = await conn.execute(
                sql.SQL("SELECT bytes,data FROM {} WHERE session_id=%s AND sha256=%s").format(
                    self.table
                ),
                (session_id, ref.sha256),
            )
            row = await cursor.fetchone()
        if row is None:
            raise PayloadNotFound(ref.sha256)
        data = bytes(row[1])
        if (
            row[0] != ref.bytes
            or len(data) != ref.bytes
            or hashlib.sha256(data).hexdigest() != ref.sha256
        ):
            raise PayloadCorrupt(ref.sha256)
        return data

    async def delete_session(self, session_id: UUID) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                sql.SQL("DELETE FROM {} WHERE session_id=%s").format(self.table), (session_id,)
            )

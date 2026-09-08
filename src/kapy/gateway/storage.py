"""Gateway-owned authorization, replay, inbox and cleanup metadata."""

import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any, LiteralString
from uuid import UUID

from psycopg import AsyncConnection, AsyncCursor, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from kapy.rpc import JsonObject, JsonValue, RpcError

from .auth import Principal, denied

# Source migration, deliberately owned here rather than in State's table registry.
TABLES = (
    "gateway_session_access (session_id uuid PRIMARY KEY, owner_id text NOT NULL, "
    "parent_session_id uuid, deleted boolean NOT NULL DEFAULT false)",
    "gateway_requests (request_id uuid PRIMARY KEY, principal_id text NOT NULL, "
    "method text NOT NULL, params_hash text NOT NULL, params jsonb NOT NULL, "
    "target_session_id uuid, operation jsonb NOT NULL DEFAULT '{}', result jsonb, error jsonb)",
    "gateway_skill_access (skill_id uuid PRIMARY KEY, creator_principal text NOT NULL, "
    "create_request_id uuid NOT NULL UNIQUE, deleted boolean NOT NULL DEFAULT false)",
    "gateway_channels (waiting_id uuid PRIMARY KEY, "
    "producer_principal text, receiver_principal text)",
    "gateway_session_cleanup (session_id uuid PRIMARY KEY, request_id uuid NOT NULL, "
    "pending_machine_ids jsonb NOT NULL, payload_pending boolean NOT NULL DEFAULT true, "
    "state text NOT NULL DEFAULT 'deleting')",
    "gateway_machine_resources (session_id uuid NOT NULL, machine_id text NOT NULL, "
    "PRIMARY KEY(session_id,machine_id))",
)


async def migrate(database_url: str, *, schema: str = "kapy_state") -> None:
    async with await AsyncConnection.connect(database_url) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
        await conn.execute(sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(schema)))
        await conn.execute("""
            DO $$ BEGIN
                IF to_regclass('gateway_channel_grants') IS NOT NULL THEN
                    RAISE EXCEPTION 'Old channel metadata is unsupported; use a new schema';
                END IF;
            END $$
        """)
        for definition in TABLES:
            await conn.execute(sql.SQL("CREATE TABLE IF NOT EXISTS " + definition))
        await conn.execute("ALTER TABLE gateway_requests ADD COLUMN IF NOT EXISTS error jsonb")


class Metadata:
    def __init__(self, pool: AsyncConnectionPool, *, schema: str) -> None:
        self.pool = pool
        self.schema = schema

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[AsyncCursor[dict[str, Any]]]:
        async with self.pool.connection() as conn, conn.transaction():
            await conn.execute(
                sql.SQL("SET LOCAL search_path TO {}").format(sql.Identifier(self.schema))
            )
            async with conn.cursor(row_factory=dict_row) as cursor:
                yield cursor

    async def rows(self, query: LiteralString, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        async with self.connection() as conn:
            result = await conn.execute(sql.SQL(query), params or None)
            return await result.fetchall() if result.description else []

    async def request(self, request_id: UUID) -> dict[str, Any] | None:
        rows = await self.rows("SELECT * FROM gateway_requests WHERE request_id=%s", (request_id,))
        return rows[0] if rows else None

    async def reserve(
        self,
        request_id: UUID,
        principal: Principal,
        method: str,
        params: JsonObject,
        target: UUID | None,
    ) -> dict[str, Any]:
        encoded = json.dumps(params, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        async with self.connection() as conn:
            await conn.execute(
                "INSERT INTO gateway_requests "
                "(request_id,principal_id,method,params_hash,params,target_session_id) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                (request_id, principal.id, method, digest, Jsonb(params), target),
            )
            cursor = await conn.execute(
                "SELECT * FROM gateway_requests WHERE request_id=%s", (request_id,)
            )
            row = await cursor.fetchone()
            assert row is not None
            if row["principal_id"] != principal.id:
                raise denied("Request belongs to another caller")
            if row["params_hash"] != digest or row["method"] != method:
                raise RpcError(-32009, "Request parameters changed", {"kind": "conflict"})
            return row

    async def finish(
        self,
        request_id: UUID,
        result: JsonValue,
        *,
        target: UUID | None = None,
        owner: str | None = None,
        parent: UUID | None = None,
        skill_id: str | None = None,
        creator: str | None = None,
    ) -> None:
        async with self.connection() as conn:
            if owner is not None:
                await conn.execute(
                    "INSERT INTO gateway_session_access(session_id,owner_id,parent_session_id) "
                    "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                    (target, owner, parent),
                )
            if skill_id is not None:
                await conn.execute(
                    "INSERT INTO gateway_skill_access"
                    "(skill_id,creator_principal,create_request_id) VALUES (%s,%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (UUID(skill_id), creator, request_id),
                )
            await conn.execute(
                "UPDATE gateway_requests SET result=%s, "
                "target_session_id=COALESCE(%s,target_session_id) WHERE request_id=%s",
                (Jsonb(result), target, request_id),
            )

    async def reject(self, request_id: UUID, error: RpcError) -> None:
        await self.rows(
            "UPDATE gateway_requests SET error=%s WHERE request_id=%s AND result IS NULL",
            (Jsonb({"code": error.code, "message": error.message, "data": error.data}), request_id),
        )

    async def operation(self, request_id: UUID, operation: JsonObject) -> None:
        await self.rows(
            "UPDATE gateway_requests SET operation=%s WHERE request_id=%s",
            (Jsonb(operation), request_id),
        )

    async def access(self, session_id: UUID) -> dict[str, Any] | None:
        rows = await self.rows(
            "SELECT * FROM gateway_session_access WHERE session_id=%s", (session_id,)
        )
        return rows[0] if rows else None

    async def authorize(self, principal: Principal, session_id: UUID) -> dict[str, Any]:
        row = await self.access(session_id)
        if row is None:
            raise denied("Session access is not established")
        allowed = principal.kind == "operator"
        if principal.kind == "session":
            allowed = session_id == principal.session_id or row["parent_session_id"] == (
                principal.session_id
            )
        elif principal.kind == "frontend":
            allowed = row["owner_id"] == principal.id
        if not allowed:
            raise denied()
        return row

    async def visible(self, principal: Principal) -> tuple[UUID, ...] | None:
        if principal.kind == "operator":
            return None
        if principal.kind == "session":
            rows = await self.rows(
                "SELECT session_id FROM gateway_session_access WHERE NOT deleted "
                "AND (session_id=%s OR parent_session_id=%s)",
                (principal.session_id, principal.session_id),
            )
        else:
            rows = await self.rows(
                "SELECT session_id FROM gateway_session_access WHERE NOT deleted AND owner_id=%s",
                (principal.id,),
            )
        return tuple(row["session_id"] for row in rows)

    async def register_channel(self, channel: UUID, *, producer: str, receiver: str | None) -> None:
        async with self.connection() as conn:
            await conn.execute(
                "INSERT INTO gateway_channels(waiting_id,producer_principal,receiver_principal) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (channel, producer, receiver),
            )
            row = await (
                await conn.execute(
                    "SELECT producer_principal,receiver_principal "
                    "FROM gateway_channels WHERE waiting_id=%s",
                    (channel,),
                )
            ).fetchone()
            if (
                not row
                or row["producer_principal"] != producer
                or row["receiver_principal"] != receiver
            ):
                raise denied("Channel endpoints are immutable")

    async def channel(
        self,
        channel: UUID,
        principal: Principal,
        *,
        create: bool = False,
        publish: bool = False,
        subscribe: bool = False,
    ) -> None:
        if create:
            await self.register_channel(channel, producer=principal.id, receiver=None)
        async with self.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT producer_principal,receiver_principal "
                    "FROM gateway_channels WHERE waiting_id=%s",
                    (channel,),
                )
            ).fetchone()
            if not row:
                if principal.kind == "operator" and publish:
                    return
                raise denied("Unknown channel")
            if principal.kind == "operator":
                return
            if publish and row["producer_principal"] != principal.id:
                raise denied("Channel belongs to another producer")
            if subscribe and row["receiver_principal"] != principal.id:
                raise denied("Channel belongs to another receiver")

    async def grant(
        self,
        channel: UUID,
        principal_id: str,
        *,
        publish: bool,
        subscribe: bool,
    ) -> None:
        # Internal registration can fill a missing endpoint, never add a second participant.
        async with self.connection() as conn:
            row = await (
                await conn.execute(
                    "SELECT producer_principal,receiver_principal "
                    "FROM gateway_channels WHERE waiting_id=%s FOR UPDATE",
                    (channel,),
                )
            ).fetchone()
            if (
                not row
                or (publish and row["producer_principal"] not in (None, principal_id))
                or (subscribe and row["receiver_principal"] not in (None, principal_id))
            ):
                raise denied("Channel endpoints are immutable")
            if publish:
                await conn.execute(
                    "UPDATE gateway_channels SET producer_principal=%s WHERE waiting_id=%s",
                    (principal_id, channel),
                )
            if subscribe:
                await conn.execute(
                    "UPDATE gateway_channels SET receiver_principal=%s WHERE waiting_id=%s",
                    (principal_id, channel),
                )

    async def begin_cleanup(
        self,
        session_id: UUID,
        request_id: UUID,
        machine_ids: Sequence[str],
    ) -> None:
        async with self.connection() as conn:
            await conn.execute(
                "INSERT INTO gateway_session_cleanup"
                "(session_id,request_id,pending_machine_ids) VALUES (%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                (session_id, request_id, Jsonb(list(machine_ids))),
            )

    async def mark_deleted(self, session_id: UUID) -> None:
        async with self.connection() as conn:
            await conn.execute(
                "UPDATE gateway_session_access SET deleted=true WHERE session_id=%s", (session_id,)
            )
            await conn.execute(
                "UPDATE gateway_session_cleanup SET state='releasing' WHERE session_id=%s",
                (session_id,),
            )

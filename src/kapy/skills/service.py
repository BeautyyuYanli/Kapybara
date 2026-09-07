"""PostgreSQL skill resources with transactional replay receipts."""

import asyncio
import hashlib
import json
from dataclasses import asdict
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg import sql
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .archive import validate_archive
from .types import SkillConflict, SkillDescription, SkillDetail, SkillInfo, SkillNotFound

_INFO_COLUMNS = sql.SQL(
    "id, name, description, revision, sha256, archive_bytes, created_at, updated_at"
)


def _validated_metadata(archive: bytes) -> tuple[str, str, str]:
    parsed = validate_archive(archive)
    return parsed.name, parsed.description, parsed.skill_md


def _info(row: dict[str, Any]) -> SkillInfo:
    return SkillInfo(
        str(row["id"]),
        row["name"],
        row["description"],
        row["revision"],
        row["sha256"],
        row["archive_bytes"],
        row["created_at"],
        row["updated_at"],
    )


class SkillService:
    def __init__(self, pool: AsyncConnectionPool, *, schema: str = "public") -> None:
        self.pool = pool
        self.schema = sql.Identifier(schema)
        self.skills = sql.Identifier(schema, "skills")
        self.requests = sql.Identifier(schema, "skill_requests")
        self._validation_slots = asyncio.Semaphore(2)

    async def initialize(self) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(self.schema))
            await conn.execute(
                sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                id uuid PRIMARY KEY, name text UNIQUE NOT NULL, description text NOT NULL,
                skill_md text NOT NULL, archive bytea NOT NULL, sha256 text NOT NULL,
                archive_bytes integer NOT NULL, revision bigint NOT NULL,
                created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now())""").format(self.skills)
            )
            await conn.execute(
                sql.SQL("""CREATE TABLE IF NOT EXISTS {} (
                request_key text PRIMARY KEY, method text NOT NULL, fingerprint text NOT NULL,
                result jsonb NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""").format(
                    self.requests
                )
            )

    async def create(self, archive: bytes, *, request_key: str) -> SkillInfo:
        result = await self._mutate("create", None, archive, None, request_key)
        assert result is not None
        return result

    async def update(
        self,
        skill_id: str,
        archive: bytes,
        *,
        expected_revision: int,
        request_key: str,
    ) -> SkillInfo:
        result = await self._mutate("update", skill_id, archive, expected_revision, request_key)
        assert result is not None
        return result

    async def delete(
        self,
        skill_id: str,
        *,
        expected_revision: int,
        request_key: str,
    ) -> None:
        await self._mutate("delete", skill_id, None, expected_revision, request_key)

    async def _mutate(
        self,
        method: str,
        skill_id: str | None,
        archive: bytes | None,
        revision: int | None,
        request_key: str,
    ) -> SkillInfo | None:
        if not request_key or len(request_key.encode()) > 512:
            raise ValueError("request_key must be nonempty and at most 512 UTF-8 bytes")
        if revision is not None and (isinstance(revision, bool) or revision < 1):
            raise ValueError("expected_revision must be positive")
        if skill_id is not None:
            skill_id = str(UUID(skill_id))
        digest = hashlib.sha256(archive).hexdigest() if archive is not None else None
        fingerprint = hashlib.sha256(
            json.dumps([method, skill_id, revision, digest], separators=(",", ":")).encode()
        ).hexdigest()
        parsed = None
        if archive is not None:
            async with self._validation_slots:
                parsed = await asyncio.to_thread(_validated_metadata, archive)
        try:
            async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
                # Serialize identical request keys before observing their receipt.
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"{self.requests}:{request_key}",),
                )
                await cur.execute(
                    sql.SQL("SELECT * FROM {} WHERE request_key=%s").format(self.requests),
                    (request_key,),
                )
                receipt = await cur.fetchone()
                if receipt is not None:
                    if receipt["fingerprint"] != fingerprint or receipt["method"] != method:
                        raise SkillConflict("request_key was already used with different arguments")
                    saved = receipt["result"]
                    if saved is None:
                        return None
                    saved["created_at"] = datetime.fromisoformat(saved["created_at"])
                    saved["updated_at"] = datetime.fromisoformat(saved["updated_at"])
                    return _info(saved)
                if method != "create":
                    await cur.execute(
                        sql.SQL("SELECT revision FROM {} WHERE id=%s FOR UPDATE").format(
                            self.skills
                        ),
                        (skill_id,),
                    )
                    current = await cur.fetchone()
                    if current is None:
                        raise SkillNotFound(skill_id)
                    if current["revision"] != revision:
                        raise SkillConflict("Skill revision has changed")
                info = None
                if method == "delete":
                    await cur.execute(
                        sql.SQL("DELETE FROM {} WHERE id=%s").format(self.skills), (skill_id,)
                    )
                else:
                    assert parsed is not None and archive is not None
                    fields = (
                        *parsed,
                        archive,
                        digest,
                        len(archive),
                    )
                    if method == "create":
                        await cur.execute(
                            sql.SQL("""INSERT INTO {} (name, description, skill_md,
                            archive, sha256, archive_bytes, id, revision)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,1) RETURNING {}""").format(
                                self.skills, _INFO_COLUMNS
                            ),
                            (*fields, uuid4()),
                        )
                    else:
                        await cur.execute(
                            sql.SQL("""UPDATE {} SET name=%s, description=%s,
                            skill_md=%s, archive=%s, sha256=%s, archive_bytes=%s,
                            revision=revision+1, updated_at=clock_timestamp()
                            WHERE id=%s RETURNING {}""").format(self.skills, _INFO_COLUMNS),
                            (*fields, skill_id),
                        )
                    row = await cur.fetchone()
                    assert row is not None
                    info = _info(row)
                result = asdict(info) if info is not None else None
                if result is not None:
                    result["created_at"] = info.created_at.isoformat()  # type: ignore[union-attr]
                    result["updated_at"] = info.updated_at.isoformat()  # type: ignore[union-attr]
                await cur.execute(
                    sql.SQL("""INSERT INTO {} (request_key, method, fingerprint,
                    result) VALUES (%s,%s,%s,%s)""").format(self.requests),
                    (request_key, method, fingerprint, Jsonb(result)),
                )
                return info
        except UniqueViolation as exc:
            raise SkillConflict("A skill with this name already exists") from exc

    async def get(self, skill_id: str) -> SkillDetail:
        row = await self._read(skill_id)
        return SkillDetail(_info(row), row["skill_md"])

    async def _read(self, skill_id: str, *, archive: bool = False) -> dict[str, Any]:
        columns = _INFO_COLUMNS + sql.SQL(", archive" if archive else ", skill_md")
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                sql.SQL("SELECT {} FROM {} WHERE id=%s").format(columns, self.skills),
                (UUID(skill_id),),
            )
            row = await cur.fetchone()
            if row is None:
                raise SkillNotFound(skill_id)
            return row

    async def catalog(
        self,
        substring: str | None = None,
        *,
        after_id: str | None = None,
        limit: int | None = None,
    ) -> tuple[SkillDescription, ...]:
        if limit is not None and (isinstance(limit, bool) or limit < 1):
            raise ValueError("limit must be positive")
        async with self.pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                sql.SQL("""SELECT id,description FROM {} WHERE
                (%s::text IS NULL OR strpos(lower(id::text),lower(%s))>0
                    OR strpos(lower(name),lower(%s))>0 OR strpos(lower(description),lower(%s))>0)
                AND (%s::uuid IS NULL OR id>%s::uuid) ORDER BY id LIMIT %s""").format(self.skills),
                (substring, substring, substring, substring, after_id, after_id, limit),
            )
            return tuple(
                SkillDescription(str(r["id"]), r["description"]) for r in await cur.fetchall()
            )

    async def download(
        self,
        skill_id: str,
        *,
        expected_revision: int | None = None,
    ) -> tuple[SkillInfo, bytes]:
        row = await self._read(skill_id, archive=True)
        if expected_revision is not None and row["revision"] != expected_revision:
            raise SkillConflict("Skill revision has changed")
        return _info(row), bytes(row["archive"])

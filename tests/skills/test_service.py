import asyncio
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from kapy.agent import AgentPayloadStore, PayloadNotFound
from kapy.skills import SkillConflict, SkillNotFound, SkillService

from .test_archive import archive  # type: ignore[missing-import]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_durable_crud_replay_and_session_payload_isolation() -> None:
    schema = f"test_intelligence_{uuid4().hex}"
    async with AsyncConnectionPool(
        "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy",
        open=False,
    ) as pool:
        try:
            service = SkillService(pool, schema=schema)
            payloads = AgentPayloadStore(pool, schema=schema)
            await service.initialize()
            await payloads.initialize()
            data = archive(description="'Literal %_ 大小写'")
            first, replay = await asyncio.gather(
                service.create(data, request_key="user:create"),
                service.create(data, request_key="user:create"),
            )
            assert first == replay
            assert len(await service.catalog("%_")) == 1
            assert not await service.catalog("missing")
            restarted = SkillService(pool, schema=schema)
            assert await restarted.download(first.id) == (first, data)
            second = await restarted.update(
                first.id,
                archive(description="changed"),
                expected_revision=1,
                request_key="user:update",
            )
            assert second.revision == 2
            with pytest.raises(SkillConflict):
                await restarted.delete(first.id, expected_revision=1, request_key="bad")
            assert await restarted.create(data, request_key="user:create") == first
            await restarted.delete(first.id, expected_revision=2, request_key="user:delete")
            await restarted.delete(first.id, expected_revision=2, request_key="user:delete")
            with pytest.raises(SkillNotFound):
                await restarted.get(first.id)
            session, other = uuid4(), uuid4()
            ref = await payloads.put(session, b"media content")
            assert await payloads.put(session, b"media content") == ref
            with pytest.raises(PayloadNotFound):
                await payloads.get(other, ref)
            assert (
                await AgentPayloadStore(pool, schema=schema).get(session, ref) == b"media content"
            )
            await payloads.delete_session(session)
            await payloads.delete_session(session)
            with pytest.raises(PayloadNotFound):
                await payloads.get(session, ref)
        finally:
            async with pool.connection() as conn:
                await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))

import asyncio
import hashlib
import threading
import weakref
from typing import cast
from uuid import uuid4

import pytest
from psycopg import AsyncCursor, sql
from psycopg_pool import AsyncConnectionPool

from kapy.agent import AgentPayloadStore, PayloadNotFound
from kapy.skills import SkillConflict, SkillNotFound, SkillService
from kapy.skills import service as skill_service
from kapy.skills.archive import Archive, validate_archive

from .test_archive import archive  # type: ignore[missing-import]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revision_race_download_snapshot_and_receipt_rollback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    schema = f"test_skill_race_{uuid4().hex}"
    async with AsyncConnectionPool(
        "postgresql://kapy:kapy-local@127.0.0.1:55432/kapy",
        open=False,
    ) as pool:
        try:
            service = SkillService(pool, schema=schema)
            await service.initialize()
            initial = await service.create(archive(), request_key="create")
            archives = {
                name: archive(description=name) for name in ("left", "right", "after-fault")
            }
            barrier = asyncio.Barrier(3)

            async def update(description: str):
                await barrier.wait()
                return await service.update(
                    initial.id,
                    archives[description],
                    expected_revision=1,
                    request_key=description,
                )

            snapshots = []

            async def download_during_race():
                await barrier.wait()
                for _ in range(4):
                    snapshots.append(await service.download(initial.id))

            results = await asyncio.gather(
                update("left"), update("right"), download_during_race(), return_exceptions=True
            )
            assert results[-1] is None
            updates = results[:2]
            assert sum(isinstance(value, SkillConflict) for value in updates) == 1
            winner = next(value for value in updates if not isinstance(value, BaseException))
            info, downloaded = await service.download(initial.id)
            assert info == winner and info.revision == 2
            assert validate_archive(downloaded).description == info.description
            assert hashlib.sha256(downloaded).hexdigest() == info.sha256
            assert downloaded == archives[info.description]
            for snapshot, payload in snapshots:
                assert snapshot in (initial, info)
                assert hashlib.sha256(payload).hexdigest() == snapshot.sha256
                assert validate_archive(payload).description == snapshot.description
            with pytest.raises(SkillConflict, match="different arguments"):
                await service.update(
                    initial.id,
                    archive(description="reused key different bytes"),
                    expected_revision=1,
                    request_key=info.description,
                )

            execute = AsyncCursor.execute

            class ReceiptFailure(Exception):
                pass

            async def fail_receipt(cur, query, *args, **kwargs):
                if isinstance(query, sql.Composable):
                    text = query.as_string(cur.connection)
                    if text.lstrip().startswith("INSERT INTO") and '"skill_requests"' in text:
                        # Observe the changed row inside this same transaction before failing.
                        await execute(
                            cur,
                            sql.SQL("SELECT revision,description FROM {} WHERE id=%s").format(
                                service.skills
                            ),
                            (initial.id,),
                        )
                        changed = await cur.fetchone()
                        assert changed["revision"] == 3 and changed["description"] == "after-fault"
                        raise ReceiptFailure
                return await execute(cur, query, *args, **kwargs)

            with monkeypatch.context() as patch:
                patch.setattr(AsyncCursor, "execute", fail_receipt)
                with pytest.raises(ReceiptFailure):
                    await service.update(
                        initial.id,
                        archives["after-fault"],
                        expected_revision=2,
                        request_key="retry-after-fault",
                    )
            assert await service.download(initial.id) == (info, downloaded)
            retried = await service.update(
                initial.id,
                archives["after-fault"],
                expected_revision=2,
                request_key="retry-after-fault",
            )
            assert retried.revision == 3
            assert await service.download(initial.id) == (
                retried,
                archives["after-fault"],
            )
        finally:
            async with pool.connection() as conn:
                await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


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
            detail = await restarted.get(first.id)
            assert detail.info == first
            assert detail.skill_md == validate_archive(data).skill_md
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


@pytest.mark.asyncio
async def test_expanded_archive_is_released_before_waiting_for_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parsed_refs: list[weakref.ReferenceType[Archive]] = []

    def track_validation(data: bytes) -> Archive:
        parsed = validate_archive(data)
        parsed_refs.append(weakref.ref(parsed))
        return parsed

    class PoolUnavailable(Exception):
        pass

    class WaitingPool:
        def connection(self) -> None:
            assert parsed_refs and parsed_refs[0]() is None
            raise PoolUnavailable

    monkeypatch.setattr(skill_service, "validate_archive", track_validation)
    service = SkillService(cast(AsyncConnectionPool, WaitingPool()))
    with pytest.raises(PoolUnavailable):
        await service.create(archive(), request_key="release-before-pool-wait")


@pytest.mark.asyncio
async def test_cancelled_validation_keeps_thread_slot_until_work_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    started = [asyncio.Event() for _ in range(3)]
    release = threading.Event()
    lock = threading.Lock()
    active = maximum = entered = 0
    original = skill_service._validated_metadata

    def blocked_validation(data: bytes) -> tuple[str, str, str]:
        nonlocal active, maximum, entered
        with lock:
            index = entered
            entered += 1
            active += 1
            maximum = max(maximum, active)
        loop.call_soon_threadsafe(started[index].set)
        try:
            release.wait()
            return original(data)
        finally:
            with lock:
                active -= 1

    class PoolUnavailable(Exception):
        pass

    class NoDatabase:
        def connection(self) -> None:
            raise PoolUnavailable

    monkeypatch.setattr(skill_service, "_validated_metadata", blocked_validation)
    service = SkillService(cast(AsyncConnectionPool, NoDatabase()))
    data = archive()
    tasks = [asyncio.create_task(service.create(data, request_key=f"cancel-{i}")) for i in range(2)]
    try:
        await asyncio.wait_for(asyncio.gather(started[0].wait(), started[1].wait()), 2)
        for task in tasks:
            task.cancel()
        await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        tasks.append(asyncio.create_task(service.create(data, request_key="third")))
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(started[2].wait(), 0.05)
        assert not tasks[0].done() and not tasks[1].done()
    finally:
        release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)
    assert isinstance(results[1], asyncio.CancelledError)
    assert isinstance(results[2], PoolUnavailable)
    assert maximum == 2

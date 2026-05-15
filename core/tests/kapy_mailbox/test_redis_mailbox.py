from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import pytest
import uuid6

import kapy_mailbox
import kapy_mailbox.redis as mailbox_redis_module
from kapy_mailbox import (
    InvalidMessageWindowError,
    MailboxAutoCompactor,
    MailboxProducerSupervisor,
    MessageFilter,
    MessageInput,
    RedisMailbox,
    RestartPolicy,
    RetentionPolicy,
)
from kapy_mailbox.exceptions import PayloadSerializationError, UnknownMessageError


@dataclass(slots=True)
class _ManualClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(
        self, *, seconds: float = 0, milliseconds: int = 0, days: int = 0
    ) -> None:
        self.current += timedelta(days=days, seconds=seconds, milliseconds=milliseconds)


@dataclass(slots=True)
class _ObservedRedis:
    """Minimal fakeredis wrapper used to assert bounded mailbox reads."""

    inner: fakeredis.aioredis.FakeRedis
    record_hmget: bool = False
    raise_on_renew_eval: bool = False
    hmget_lengths: list[int] = field(default_factory=list)

    async def hmget(self, name: str, keys: list[str]):
        if self.record_hmget:
            self.hmget_lengths.append(len(keys))
        return await self.inner.hmget(name, keys)

    async def eval(self, script: str, numkeys: int, *keys_and_args: object):
        if (
            self.raise_on_renew_eval
            and script == mailbox_redis_module._RENEW_COMPACT_LOCK_SCRIPT
        ):
            raise RuntimeError("renew failed")
        return await self.inner.eval(script, numkeys, *keys_and_args)

    async def aclose(self) -> None:
        await self.inner.aclose()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _make_mailbox(
    *,
    retention: RetentionPolicy | None = None,
    observe_reads: bool = False,
) -> tuple[RedisMailbox, _ManualClock, _ObservedRedis]:
    clock = _ManualClock(datetime(2026, 5, 15, 12, 0, 0, tzinfo=UTC))
    raw_redis = fakeredis.aioredis.FakeRedis()
    redis = _ObservedRedis(raw_redis, record_hmget=observe_reads)
    mailbox = RedisMailbox(redis, namespace="tests", clock=clock, retention=retention)
    return mailbox, clock, redis


def _assert_compaction_result_public_shape(result: object) -> None:
    assert not hasattr(result, "stream_entries_trimmed")


@pytest.mark.anyio
async def test_put_many_preserves_order_and_get_uses_default_limit() -> None:
    mailbox, clock, redis = _make_mailbox(observe_reads=True)

    items = []
    for index in range(105):
        items.append(
            MessageInput(
                channel=f"telegram/chat/{index % 2}",
                payload={"index": index},
            )
        )
        clock.advance(milliseconds=1)

    messages = await mailbox.put_many(items, producer="telegram")

    assert [message.payload["index"] for message in messages[:3]] == [0, 1, 2]
    assert [message.id for message in messages] == sorted(
        message.id for message in messages
    )
    assert len(await mailbox.get()) == 100
    assert len(await mailbox.get(limit=None)) == 105
    newest = await mailbox.get(limit=3, order="newest_first")
    assert [message.payload["index"] for message in newest] == [104, 103, 102]
    assert max(redis.hmget_lengths) <= 100
    await redis.aclose()


@pytest.mark.anyio
async def test_get_supports_subtree_exact_and_consumed_filters() -> None:
    mailbox, clock, redis = _make_mailbox()

    root = await mailbox.put("telegram/chat/1", {"kind": "root"})
    clock.advance(milliseconds=1)
    child = await mailbox.put("telegram/chat/1/thread/2", {"kind": "child"})
    clock.advance(milliseconds=1)
    other = await mailbox.put("telegram/chat/2", {"kind": "other"})
    _ = other

    subtree = await mailbox.get(MessageFilter(channels=frozenset({"telegram/chat/1"})))
    exact = await mailbox.get(
        MessageFilter(
            channels=frozenset({"telegram/chat/1"}),
            channel_match="exact",
        )
    )
    await mailbox.consume([child.id], consumer="agent")
    unconsumed = await mailbox.get(MessageFilter(consumed=False), limit=None)
    consumed = await mailbox.get(MessageFilter(consumed=True), limit=None)

    assert [message.id for message in subtree] == [root.id, child.id]
    assert [message.id for message in exact] == [root.id]
    assert [message.id for message in unconsumed] == [root.id, other.id]
    assert [message.id for message in consumed] == [child.id]
    await redis.aclose()


@pytest.mark.anyio
async def test_consume_reports_consumed_already_consumed_and_not_found() -> None:
    mailbox, _, redis = _make_mailbox()
    first = await mailbox.put("telegram/chat/1", {"index": 1})
    second = await mailbox.put("telegram/chat/1", {"index": 2})
    missing_id = str(uuid6.uuid7())

    first_result = await mailbox.consume([first.id], consumer="agent")
    second_result = await mailbox.consume(
        [first.id, second.id, missing_id],
        consumer="agent",
    )

    assert first_result.consumed == [first.id]
    assert first_result.already_consumed == []
    assert first_result.not_found == []
    assert second_result.consumed == [second.id]
    assert second_result.already_consumed == [first.id]
    assert second_result.not_found == [missing_id]
    await redis.aclose()


@pytest.mark.anyio
async def test_consume_strict_raises_for_unknown_ids() -> None:
    mailbox, _, redis = _make_mailbox()
    message = await mailbox.put("telegram/chat/1", {"index": 1})
    missing_id = str(uuid6.uuid7())

    with pytest.raises(UnknownMessageError) as exc_info:
        await mailbox.consume([message.id, missing_id], strict=True)

    assert exc_info.value.message_ids == [missing_id]
    assert await redis.hget(mailbox._keys.consumed_info, message.id) is None
    assert await redis.zscore(mailbox._keys.consumed, message.id) is None
    await redis.aclose()


@pytest.mark.anyio
async def test_get_multi_channel_pagination_keeps_overflow_candidates() -> None:
    mailbox, clock, redis = _make_mailbox()

    for index in range(4):
        await mailbox.put(
            "telegram/chat/a",
            {"index": index},
            producer="drop",
        )
        clock.advance(milliseconds=1)
    expected_ids: list[str] = []
    for index in range(4):
        message = await mailbox.put(
            "telegram/chat/b",
            {"index": index},
            producer="keep",
        )
        expected_ids.append(message.id)
        clock.advance(milliseconds=1)

    messages = await mailbox.get(
        MessageFilter(
            channels=frozenset({"telegram/chat/a", "telegram/chat/b"}),
            producer="keep",
        ),
        limit=4,
        order="oldest_first",
    )

    assert [message.id for message in messages] == expected_ids
    await redis.aclose()


@pytest.mark.anyio
async def test_put_rejects_non_json_payloads_by_default() -> None:
    mailbox, _, redis = _make_mailbox()

    with pytest.raises(PayloadSerializationError):
        await mailbox.put("telegram/chat/1", object())  # type: ignore[arg-type]
    await redis.aclose()


def test_public_mailbox_api_removes_watch_surface() -> None:
    assert not hasattr(RedisMailbox, "watch")
    assert not hasattr(kapy_mailbox, "MailboxWatcher")
    assert not hasattr(kapy_mailbox, "WatchAfter")
    assert not hasattr(kapy_mailbox, "WatchStart")
    assert not hasattr(kapy_mailbox, "WatchClosedError")


@pytest.mark.anyio
async def test_put_does_not_create_legacy_watch_event_stream() -> None:
    mailbox, _, redis = _make_mailbox()

    await mailbox.put("telegram/chat/1", {"index": 1})

    assert await redis.exists("mailbox:tests:events") == 0
    await redis.aclose()


@pytest.mark.anyio
async def test_compact_deletes_legacy_watch_event_stream_even_without_messages() -> (
    None
):
    mailbox, _, redis = _make_mailbox()
    legacy_events_key = "mailbox:tests:events"

    await redis.xadd(legacy_events_key, {"message_id": "legacy"})

    result = await mailbox.compact()

    assert result.messages_deleted == 0
    _assert_compaction_result_public_shape(result)
    assert await redis.exists(legacy_events_key) == 0
    await redis.aclose()


@pytest.mark.anyio
async def test_get_after_missing_id_uses_pure_cursor_semantics() -> None:
    mailbox, clock, redis = _make_mailbox()
    first = await mailbox.put("telegram/chat/1", {"index": 1})
    clock.advance(milliseconds=1)
    second = await mailbox.put("telegram/chat/1", {"index": 2})
    missing_id = str(uuid6.UUID(int=uuid6.UUID(first.id).int + 1, version=7))

    received = await mailbox.get(after_id=missing_id, limit=None)

    assert [message.id for message in received] == [second.id]

    later_cursor = str(uuid6.UUID(int=uuid6.UUID(second.id).int + 1, version=7))
    assert await mailbox.get(after_id=second.id, limit=None) == []
    assert await mailbox.get(after_id=later_cursor, limit=None) == []
    await redis.aclose()


@pytest.mark.anyio
async def test_get_after_invalid_uuid_raises_window_error() -> None:
    mailbox, _, redis = _make_mailbox()

    with pytest.raises(InvalidMessageWindowError):
        await mailbox.get(after_id="not-a-uuid")

    await redis.aclose()


@pytest.mark.anyio
async def test_get_after_existing_id_keeps_pure_cursor_semantics_if_id_is_compacted() -> (
    None
):
    mailbox, clock, redis = _make_mailbox()
    first = await mailbox.put("telegram/chat/1", {"index": 1})
    clock.advance(milliseconds=1)
    second = await mailbox.put("telegram/chat/1", {"index": 2})
    clock.advance(milliseconds=1)
    third = await mailbox.put("telegram/chat/1", {"index": 3})

    await redis.hdel(mailbox._keys.messages, first.id)
    await redis.zrem(mailbox._keys.timeline, first.id)
    await redis.zrem(mailbox._keys.channel_exact(first.channel), first.id)
    for prefix in mailbox_redis_module.iter_channel_prefixes(first.channel):
        await redis.zrem(mailbox._keys.channel_prefix(prefix), first.id)
    await redis.zrem(mailbox._keys.unconsumed, first.id)

    received = await mailbox.get(after_id=first.id, limit=10)

    assert [message.id for message in received] == [second.id, third.id]
    await redis.aclose()


@pytest.mark.anyio
async def test_created_at_is_derived_from_generated_uuid_timestamp() -> None:
    mailbox, clock, redis = _make_mailbox()
    clock.current = datetime(2026, 5, 15, 13, 14, 15, 123000, tzinfo=UTC)

    message = await mailbox.put("telegram/chat/1", {"index": 1})

    assert message.created_at == datetime.fromtimestamp(
        uuid6.UUID(message.id).time / 1000,
        tz=UTC,
    )
    await redis.aclose()


@pytest.mark.anyio
async def test_consume_missing_message_does_not_create_orphaned_indexes() -> None:
    mailbox, _, redis = _make_mailbox()
    message = await mailbox.put("telegram/chat/1", {"index": 1})
    await redis.hdel(mailbox._keys.messages, message.id)
    await redis.zrem(mailbox._keys.timeline, message.id)
    await redis.zrem(mailbox._keys.unconsumed, message.id)

    result = await mailbox.consume([message.id])

    assert result.not_found == [message.id]
    assert await redis.hget(mailbox._keys.consumed_info, message.id) is None
    assert await redis.zscore(mailbox._keys.consumed, message.id) is None
    await redis.aclose()


@pytest.mark.anyio
async def test_compact_respects_keep_unconsumed_flag() -> None:
    mailbox, clock, redis = _make_mailbox(
        retention=RetentionPolicy(max_age=timedelta(days=3), keep_unconsumed=True)
    )
    old_unconsumed = await mailbox.put("telegram/chat/1", {"index": 1})
    clock.advance(days=1, milliseconds=1)
    old_consumed = await mailbox.put("telegram/chat/1", {"index": 2})
    await mailbox.consume([old_consumed.id], consumer="agent")
    clock.advance(days=4)

    result = await mailbox.compact()
    remaining = await mailbox.get(limit=None)

    assert result.messages_deleted == 1
    _assert_compaction_result_public_shape(result)
    assert result.skipped_unconsumed == 1
    assert [message.id for message in remaining] == [old_unconsumed.id]
    await redis.aclose()


@pytest.mark.anyio
async def test_compaction_lock_scripts_execute_real_lua() -> None:
    mailbox, _, redis = _make_mailbox()
    lock_key = mailbox._keys.compact_lock

    await redis.set(lock_key, "owner-a")

    wrong_renew = await redis.eval(
        mailbox_redis_module._RENEW_COMPACT_LOCK_SCRIPT,
        1,
        lock_key,
        "owner-b",
        60_000,
    )
    right_renew = await redis.eval(
        mailbox_redis_module._RENEW_COMPACT_LOCK_SCRIPT,
        1,
        lock_key,
        "owner-a",
        60_000,
    )
    wrong_release = await redis.eval(
        mailbox_redis_module._RELEASE_COMPACT_LOCK_SCRIPT,
        1,
        lock_key,
        "owner-b",
    )
    right_release = await redis.eval(
        mailbox_redis_module._RELEASE_COMPACT_LOCK_SCRIPT,
        1,
        lock_key,
        "owner-a",
    )

    assert wrong_renew == 0
    assert right_renew == 1
    assert wrong_release == 0
    assert right_release == 1
    assert await redis.get(lock_key) is None
    await redis.aclose()


@pytest.mark.anyio
async def test_auto_compactor_runs_periodically() -> None:
    mailbox, clock, redis = _make_mailbox(
        retention=RetentionPolicy(max_age=timedelta(days=3), keep_unconsumed=False)
    )
    await mailbox.put("telegram/chat/1", {"index": 1})
    clock.advance(days=4)

    compactor = MailboxAutoCompactor(
        mailbox,
        interval=timedelta(milliseconds=10),
        jitter_ratio=0.0,
    )
    await compactor.start()
    await asyncio.sleep(0.05)
    await compactor.stop()

    assert await mailbox.get(limit=None) == []
    await redis.aclose()


@pytest.mark.anyio
async def test_compact_stops_after_batch_boundary_when_should_continue_turns_false() -> (
    None
):
    mailbox, clock, redis = _make_mailbox(
        retention=RetentionPolicy(max_age=timedelta(days=3), keep_unconsumed=False)
    )
    for index in range(4):
        await mailbox.put("telegram/chat/1", {"index": index})
        clock.advance(days=4, milliseconds=1)

    should_continue_calls = 0

    def should_continue() -> bool:
        nonlocal should_continue_calls
        should_continue_calls += 1
        return should_continue_calls < 2

    result = await mailbox.compact(
        _should_continue=should_continue, _delete_batch_size=1
    )
    remaining = await mailbox.get(limit=None)

    assert result.messages_deleted == 1
    _assert_compaction_result_public_shape(result)
    assert len(remaining) == 3
    await redis.aclose()


@pytest.mark.anyio
async def test_auto_compactor_lock_renewal_exception_sets_lock_lost_and_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mailbox, _, redis = _make_mailbox()
    compactor = MailboxAutoCompactor(
        mailbox,
        lock_renew_interval=timedelta(milliseconds=10),
    )
    lock_lost = asyncio.Event()
    caplog.set_level("ERROR")
    redis.raise_on_renew_eval = True

    await compactor._renew_lock("token", lock_lost)

    assert lock_lost.is_set()
    assert "Mailbox compaction lock renewal failed" in caplog.text
    await redis.aclose()


@pytest.mark.anyio
async def test_producer_supervisor_restarts_failed_producer_and_stops_cleanly() -> None:
    mailbox, _, redis = _make_mailbox()
    supervisor = MailboxProducerSupervisor(
        mailbox.writer(),
        restart_policy=RestartPolicy(
            initial_delay_seconds=0.01,
            max_delay_seconds=0.02,
            multiplier=1.0,
            jitter_ratio=0.0,
        ),
    )
    attempts: list[int] = []

    async def producer(writer: Any, token: Any) -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        await writer.put("telegram/chat/1", {"attempt": len(attempts)}, producer="demo")
        await token.wait_cancelled()

    supervisor.register_producer("demo", producer)
    await supervisor.start()

    for _ in range(100):
        if len(await mailbox.get(limit=None)) == 1:
            break
        await asyncio.sleep(0.01)

    status_before_stop = supervisor.status("demo")
    await supervisor.stop(timeout=0.5)
    status_after_stop = supervisor.status("demo")

    assert len(attempts) >= 2
    assert status_before_stop.restart_count >= 1
    assert status_before_stop.state in {"running", "backoff", "stopping"}
    assert status_after_stop.state == "stopped"
    await redis.aclose()

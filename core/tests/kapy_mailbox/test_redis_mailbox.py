from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import pytest
import uuid6
from redis.exceptions import ConnectionError as RedisConnectionError

import kapy_mailbox.redis as mailbox_redis_module
from kapy_mailbox import (
    InvalidMessageFilterError,
    InvalidMessageWindowError,
    MailboxAutoCompactor,
    MailboxProducerSupervisor,
    MessageFilter,
    MessageInput,
    RedisMailbox,
    RestartPolicy,
    RetentionPolicy,
    WatchStart,
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
    hmget_failures: list[Exception] = field(default_factory=list)
    xread_failures: list[Exception] = field(default_factory=list)

    async def hmget(self, name: str, keys: list[str]):
        if self.hmget_failures:
            raise self.hmget_failures.pop(0)
        if self.record_hmget:
            self.hmget_lengths.append(len(keys))
        return await self.inner.hmget(name, keys)

    async def xread(
        self,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ):
        if self.xread_failures:
            raise self.xread_failures.pop(0)
        return await self.inner.xread(streams, count=count, block=block)

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


@pytest.mark.anyio
async def test_watch_oldest_drains_history_then_waits_for_future_message() -> None:
    mailbox, clock, redis = _make_mailbox()
    first = await mailbox.put("telegram/chat/1", {"index": 1})
    clock.advance(milliseconds=1)
    second = await mailbox.put("telegram/chat/1", {"index": 2})

    async with await mailbox.watch(
        MessageFilter(channels=frozenset({"telegram/chat/1"})),
        start="oldest",
        batch_size=2,
    ) as watcher:
        history = await watcher.try_next()
        assert [message.id for message in history] == [first.id, second.id]
        assert await watcher.try_next() == []

        async def _publish() -> None:
            await asyncio.sleep(0.01)
            clock.advance(milliseconds=1)
            await mailbox.put("telegram/chat/1", {"index": 3})

        task = asyncio.create_task(_publish())
        live_batch = await asyncio.wait_for(watcher.next(), timeout=1)
        await task
        assert [message.payload["index"] for message in live_batch] == [3]
    await redis.aclose()


@pytest.mark.anyio
async def test_watch_new_anchors_at_creation_not_first_read() -> None:
    mailbox, clock, redis = _make_mailbox()

    watcher = await mailbox.watch(start="new")
    clock.advance(milliseconds=1)
    created = await mailbox.put("telegram/chat/1", {"index": 1})

    received = await watcher.try_next()

    assert [message.id for message in received] == [created.id]
    await watcher.close()
    await redis.aclose()


@pytest.mark.anyio
async def test_next_retries_recoverable_query_failure_and_preserves_cursor() -> None:
    mailbox, clock, redis = _make_mailbox()
    first = await mailbox.put("telegram/chat/1", {"index": 1})
    clock.advance(milliseconds=1)
    await mailbox.put("telegram/chat/1", {"index": 2})
    redis.hmget_failures.append(RedisConnectionError("temporary read failure"))

    watcher = await mailbox.watch(start="oldest")
    received = await watcher.next()

    assert [message.id for message in received] == [first.id]
    await watcher.close()
    await redis.aclose()


@pytest.mark.anyio
async def test_next_retries_recoverable_blocking_read_failure_then_succeeds() -> None:
    mailbox, clock, redis = _make_mailbox()
    redis.xread_failures.append(RedisConnectionError("temporary xread failure"))
    watcher = await mailbox.watch(start="new")

    async def _publish() -> None:
        await asyncio.sleep(0.2)
        clock.advance(milliseconds=1)
        await mailbox.put("telegram/chat/1", {"index": 1})

    task = asyncio.create_task(_publish())
    received = await asyncio.wait_for(watcher.next(), timeout=2)
    await task

    assert [message.payload["index"] for message in received] == [1]
    await watcher.close()
    await redis.aclose()


@pytest.mark.anyio
async def test_next_exhausts_retries_and_raises_last_connection_error() -> None:
    mailbox, _, redis = _make_mailbox()
    await mailbox.put("telegram/chat/1", {"index": 1})
    redis.hmget_failures.extend(
        [RedisConnectionError(f"failure-{index}") for index in range(4)]
    )
    watcher = await mailbox.watch(start="oldest")

    with pytest.raises(RedisConnectionError, match="failure-3"):
        await watcher.next()

    await watcher.close()
    await redis.aclose()


@pytest.mark.anyio
async def test_try_next_does_not_retry_recoverable_connection_error() -> None:
    mailbox, _, redis = _make_mailbox()
    await mailbox.put("telegram/chat/1", {"index": 1})
    redis.hmget_failures.append(RedisConnectionError("try-next failure"))
    watcher = await mailbox.watch(start="oldest")

    with pytest.raises(RedisConnectionError, match="try-next failure"):
        await watcher.try_next()

    assert len(redis.hmget_failures) == 0
    await watcher.close()
    await redis.aclose()


@pytest.mark.anyio
async def test_watch_rejects_consumed_true_filter() -> None:
    mailbox, _, redis = _make_mailbox()

    with pytest.raises(InvalidMessageFilterError):
        await mailbox.watch(
            MessageFilter(consumed=True),
        )

    await redis.aclose()


@pytest.mark.anyio
async def test_watch_after_missing_id_uses_pure_cursor_semantics() -> None:
    mailbox, clock, redis = _make_mailbox()
    first = await mailbox.put("telegram/chat/1", {"index": 1})
    clock.advance(milliseconds=1)
    second = await mailbox.put("telegram/chat/1", {"index": 2})
    missing_id = str(uuid6.UUID(int=uuid6.UUID(first.id).int + 1, version=7))
    assert missing_id not in {first.id, second.id}

    watcher = await mailbox.watch(start=WatchStart.after(missing_id))
    received = await watcher.try_next()
    await watcher.close()

    assert [message.id for message in received] == [second.id]

    later_cursor = str(uuid6.UUID(int=uuid6.UUID(second.id).int + 1, version=7))
    watcher_after_created = await mailbox.watch(start=WatchStart.after(second.id))
    watcher_after_future = await mailbox.watch(start=WatchStart.after(later_cursor))

    received_after_created = await watcher_after_created.try_next()
    received_after_future = await watcher_after_future.try_next()

    assert received_after_created == []
    assert received_after_future == []
    await watcher_after_created.close()
    await watcher_after_future.close()
    await redis.aclose()


@pytest.mark.anyio
async def test_watch_after_missing_id_logs_no_warning_with_pure_cursor_semantics(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mailbox, clock, redis = _make_mailbox()
    first = await mailbox.put("telegram/chat/1", {"index": 1})
    clock.advance(milliseconds=1)
    second = await mailbox.put("telegram/chat/1", {"index": 2})
    missing_id = str(uuid6.UUID(int=uuid6.UUID(first.id).int + 1, version=7))
    caplog.set_level("WARNING")

    watcher = await mailbox.watch(start=WatchStart.after(missing_id))
    received = await watcher.try_next()

    assert [message.id for message in received] == [second.id]
    assert caplog.text == ""
    await watcher.close()
    await redis.aclose()


@pytest.mark.anyio
async def test_watch_after_invalid_uuid_raises_window_error() -> None:
    mailbox, _, redis = _make_mailbox()

    with pytest.raises(InvalidMessageWindowError):
        await mailbox.watch(start=WatchStart.after("not-a-uuid"))
    await redis.aclose()


@pytest.mark.anyio
async def test_watch_after_existing_id_keeps_resume_cursor_even_if_id_is_compacted() -> (
    None
):
    mailbox, clock, redis = _make_mailbox()
    first = await mailbox.put("telegram/chat/1", {"index": 1})
    clock.advance(milliseconds=1)
    second = await mailbox.put("telegram/chat/1", {"index": 2})
    clock.advance(milliseconds=1)
    third = await mailbox.put("telegram/chat/1", {"index": 3})

    watcher = await mailbox.watch(start=WatchStart.after(first.id), batch_size=10)

    await redis.hdel(mailbox._keys.messages, first.id)
    await redis.zrem(mailbox._keys.timeline, first.id)
    await redis.zrem(mailbox._keys.channel_exact(first.channel), first.id)
    for prefix in mailbox_redis_module.iter_channel_prefixes(first.channel):
        await redis.zrem(mailbox._keys.channel_prefix(prefix), first.id)
    await redis.zrem(mailbox._keys.unconsumed, first.id)

    received = await watcher.try_next()

    assert [message.id for message in received] == [second.id, third.id]
    await watcher.close()
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
    assert len(remaining) == 3
    await redis.aclose()


@pytest.mark.anyio
async def test_auto_compactor_lock_renewal_exception_sets_lock_lost_and_logs(
    monkeypatch: pytest.MonkeyPatch,
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

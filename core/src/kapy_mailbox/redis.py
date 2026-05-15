"""Redis-backed mailbox implementation.

The core `RedisMailbox` is intentionally a thin API wrapper around an injected
`redis.asyncio.Redis` client. It does not own the Redis connection, does not
close it, and does not start background tasks. Watchers, producer supervision,
and periodic compaction are separate lifecycle objects so callers can compose
them explicitly.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from logging import Logger, getLogger
from typing import Protocol, cast
from urllib.parse import quote

import uuid6
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError

from k.agent.channels import iter_channel_prefixes, validate_channel_path
from kapy_mailbox.exceptions import (
    AutoCompactorClosedError,
    InvalidChannelError,
    InvalidMessageFilterError,
    InvalidMessageWindowError,
    ProducerAlreadyRegisteredError,
    ProducerSupervisorClosedError,
    UnknownMessageError,
    WatchClosedError,
)
from kapy_mailbox.models import (
    CancellationToken,
    CompactionResult,
    ConsumeResult,
    JsonValue,
    Message,
    MessageFilter,
    MessageInput,
    MessageOrder,
    ProducerHandle,
    ProducerStatus,
    RestartPolicy,
    RetentionPolicy,
    WatchAfter,
    WatchStartValue,
    _ProducerRuntime,
)
from kapy_mailbox.serialization import JSONMessageSerializer, MessageSerializer

logger = getLogger(__name__)

DEFAULT_COMPACT_INTERVAL = timedelta(minutes=15)
DEFAULT_COMPACT_LOCK_TTL = timedelta(minutes=5)
DEFAULT_COMPACT_LOCK_RENEW_INTERVAL = timedelta(seconds=60)
DEFAULT_QUERY_PAGE_SIZE = 100
DEFAULT_COMPACT_BATCH_SIZE = 100
WATCHER_RETRY_DELAYS_SECONDS = (0.1, 0.5, 1.0)

_CONSUME_MESSAGES_SCRIPT = """
local messages_key = KEYS[1]
local unconsumed_key = KEYS[2]
local consumed_key = KEYS[3]
local consumed_info_key = KEYS[4]
local consumed_info_json = ARGV[1]
local strict = ARGV[2] == '1'

local not_found = {}
if strict then
    for index = 3, #ARGV do
        local message_id = ARGV[index]
        if redis.call('HEXISTS', messages_key, message_id) == 0 then
            table.insert(not_found, message_id)
        end
    end
    if #not_found > 0 then
        return { {}, {}, not_found }
    end
end

local consumed = {}
local already_consumed = {}
if not strict then
    not_found = {}
end

for index = 3, #ARGV do
    local message_id = ARGV[index]
    if redis.call('HEXISTS', messages_key, message_id) == 0 then
        table.insert(not_found, message_id)
    elseif redis.call('HEXISTS', consumed_info_key, message_id) == 1 then
        table.insert(already_consumed, message_id)
    else
        redis.call('HSET', consumed_info_key, message_id, consumed_info_json)
        redis.call('ZREM', unconsumed_key, message_id)
        redis.call('ZADD', consumed_key, 0, message_id)
        table.insert(consumed, message_id)
    end
end

return { consumed, already_consumed, not_found }
"""

_RENEW_COMPACT_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_RELEASE_COMPACT_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

type MailboxProducer = Callable[[MailboxWriter, CancellationToken], Awaitable[None]]


class Clock(Protocol):
    """Clock protocol used by the mailbox for deterministic tests."""

    def now(self) -> datetime:
        """Return the current time."""

        ...


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _UUIDv7Factory:
    """Generate UUIDv7 ids from the mailbox clock.

    `uuid6.uuid7()` is used directly for the normal system-clock path. When tests
    inject a custom clock, the factory builds a UUIDv7 through `uuid6.UUID` so the
    id timestamp and derived `created_at` stay aligned with that clock while still
    preserving monotonic ordering within the process.
    """

    def __init__(self, *, clock: Clock) -> None:
        self._clock = clock
        self._last_milliseconds = -1
        self._last_random_bits = 0

    def new(self) -> tuple[str, datetime]:
        if isinstance(self._clock, _SystemClock):
            value = uuid6.uuid7()
        else:
            now = _normalize_datetime(self._clock.now())
            milliseconds = int(now.timestamp() * 1000)
            random_bits = random.getrandbits(74)

            if milliseconds < self._last_milliseconds:
                milliseconds = self._last_milliseconds
            if milliseconds == self._last_milliseconds:
                random_bits = self._last_random_bits + 1
                if random_bits >= 1 << 74:
                    milliseconds += 1
                    random_bits = 0

            self._last_milliseconds = milliseconds
            self._last_random_bits = random_bits

            uuid_int = (
                (milliseconds << 80)
                | (0x7 << 76)
                | (((random_bits >> 62) & 0xFFF) << 64)
                | (0b10 << 62)
                | (random_bits & ((1 << 62) - 1))
            )
            value = uuid6.UUID(int=uuid_int, version=7)
        created_at = datetime.fromtimestamp(value.time / 1000, tz=UTC)
        return str(value), created_at


@dataclass(slots=True, frozen=True)
class _MailboxKeys:
    """Redis key layout for one mailbox namespace."""

    namespace: str

    @property
    def messages(self) -> str:
        return f"mailbox:{self.namespace}:messages"

    @property
    def timeline(self) -> str:
        return f"mailbox:{self.namespace}:timeline"

    @property
    def unconsumed(self) -> str:
        return f"mailbox:{self.namespace}:unconsumed"

    @property
    def consumed(self) -> str:
        return f"mailbox:{self.namespace}:consumed"

    @property
    def consumed_info(self) -> str:
        return f"mailbox:{self.namespace}:consumed_info"

    @property
    def events(self) -> str:
        return f"mailbox:{self.namespace}:events"

    @property
    def compact_lock(self) -> str:
        return f"mailbox:{self.namespace}:compact_lock"

    def channel_exact(self, channel: str) -> str:
        return f"mailbox:{self.namespace}:channel_exact:{quote(channel, safe='')}"

    def channel_prefix(self, prefix: str) -> str:
        return f"mailbox:{self.namespace}:channel_prefix:{quote(prefix, safe='')}"


@dataclass(slots=True)
class _CandidateSource:
    """Mutable cursor for one Redis lexicographic index scan."""

    key: str
    after_id: str | None = None
    cursor: str | None = None
    exhausted: bool = False


@dataclass(slots=True)
class _CandidateScanState:
    """State for bounded multi-index mailbox scans.

    Channel filters can require several Redis reads because multiple indexes may
    overlap. The scan state keeps per-index cursors and a de-duplication set so
    `get(limit=...)` stays bounded at the Redis range-read layer.
    """

    sources: list[_CandidateSource]
    seen_ids: set[str]
    pending_ids: list[str]


class RedisMailbox:
    """Lightweight Redis-backed mailbox API wrapper.

    This object is safe to share across concurrent producers, watchers, and
    consumers as long as the injected Redis client supports concurrent async I/O.
    It does not own the Redis client lifecycle and intentionally starts no
    background tasks on its own.
    """

    def __init__(
        self,
        redis: Redis,
        *,
        namespace: str,
        serializer: MessageSerializer | None = None,
        clock: Clock | None = None,
        retention: RetentionPolicy | None = None,
    ) -> None:
        self._redis = redis
        self._namespace = namespace
        self._keys = _MailboxKeys(namespace)
        self._serializer = serializer or JSONMessageSerializer()
        self._clock = clock or _SystemClock()
        self._uuid_factory = _UUIDv7Factory(clock=self._clock)
        self._retention = retention or RetentionPolicy()

    def writer(self) -> MailboxWriter:
        """Return a write-only facade for supervised producers."""

        return MailboxWriter(self)

    async def put(
        self,
        channel: str,
        payload: JsonValue,
        *,
        producer: str | None = None,
    ) -> Message:
        """Append one message to the mailbox and return its snapshot."""

        messages = await self.put_many(
            [MessageInput(channel=channel, payload=payload, producer=producer)],
        )
        return messages[0]

    async def put_many(
        self,
        items: Iterable[MessageInput],
        *,
        producer: str | None = None,
    ) -> list[Message]:
        """Append several messages with one Redis transaction."""

        normalized_items = list(items)
        if not normalized_items:
            return []

        messages: list[Message] = []
        for index, item in enumerate(normalized_items):
            channel = self._normalize_channel(
                item.channel,
                field_name=f"items[{index}].channel",
            )
            normalized_payload = self._serializer.normalize_payload(item.payload)
            message_id, created_at = self._uuid_factory.new()
            effective_producer = (
                item.producer if item.producer is not None else producer
            )
            messages.append(
                Message(
                    id=message_id,
                    channel=channel,
                    payload=normalized_payload,
                    created_at=created_at,
                    producer=effective_producer,
                )
            )

        pipeline = self._redis.pipeline(transaction=True)
        for message in messages:
            pipeline.hset(
                self._keys.messages,
                key=message.id,
                value=self._serializer.dump_message(message),
            )
            pipeline.zadd(self._keys.timeline, {message.id: 0.0})
            pipeline.zadd(self._keys.channel_exact(message.channel), {message.id: 0.0})
            for prefix in iter_channel_prefixes(message.channel):
                pipeline.zadd(self._keys.channel_prefix(prefix), {message.id: 0.0})
            pipeline.zadd(self._keys.unconsumed, {message.id: 0.0})
            pipeline.xadd(
                self._keys.events,
                {"message_id": message.id, "channel": message.channel},
            )
        await pipeline.execute()
        return messages

    async def get(
        self,
        filter: MessageFilter | None = None,
        *,
        after_id: str | None = None,
        limit: int | None = 100,
        order: MessageOrder = "oldest_first",
    ) -> list[Message]:
        """Return message snapshots matching the filter and read window.

        The default `limit=100` is a safety window: callers must pass
        `limit=None` explicitly to request an unbounded scan. `after_id` resumes
        strictly after a UUIDv7 cursor, and `order` chooses oldest-first or
        newest-first lexicographic UUID order. The returned messages are loaded as
        immutable snapshots, so mutating them does not affect Redis state.
        """

        normalized_after_id = self._normalize_after_id(after_id)
        if limit is not None and limit < 0:
            raise InvalidMessageWindowError("limit must be >= 0 or None")
        if order not in {"oldest_first", "newest_first"}:
            raise InvalidMessageWindowError(f"Unsupported order: {order!r}")
        if limit == 0:
            return []

        message_filter = filter or MessageFilter()
        scan_state = self._candidate_scan_state(
            message_filter,
            after_id=normalized_after_id,
            order=order,
        )

        matched: list[Message] = []
        while True:
            remaining = None if limit is None else limit - len(matched)
            if remaining == 0:
                return matched

            candidate_ids = await self._candidate_ids_page(
                scan_state,
                order=order,
                limit=_query_page_size(remaining),
            )
            if not candidate_ids:
                return matched

            messages = await self._load_messages(candidate_ids)
            for message in messages:
                if not message_filter.matches(message):
                    continue
                matched.append(message)
                if limit is not None and len(matched) >= limit:
                    return matched

    async def watch(
        self,
        filter: MessageFilter | None = None,
        *,
        start: WatchStartValue = "new",
        batch_size: int = 1,
    ) -> MailboxWatcher:
        """Create an independent watcher session.

        This method is async so it can resolve its starting cursor before the
        watcher is returned. `start="new"` therefore anchors against mailbox
        state at watch-creation time instead of first read, which avoids races
        with concurrent puts.

        `start="new"` begins strictly after the mailbox's current latest id.
        `start="oldest"` replays matching historical messages first. A
        `WatchStart.after(id)` resume cursor must be a valid UUIDv7 string, but
        it otherwise uses pure cursor semantics: the id does not need to exist in
        Redis, belong to this namespace, or still be present after compaction.

        Live watch observes message arrival rather than later consume-state
        transitions. Because the mailbox does not publish consume-transition
        events, `MessageFilter(consumed=True)` is rejected for `watch()` and must
        be expressed through `get(...)` instead.
        """

        resolved_filter = filter or MessageFilter()
        if resolved_filter.consumed is True:
            raise InvalidMessageFilterError(
                "watch() does not support MessageFilter(consumed=True); use get() for consumed history"
            )

        initial_stream_anchor = await self._latest_stream_id()
        resolved_last_seen_id: str | None = None

        if start == "oldest":
            resolved_last_seen_id = None
        elif start == "new":
            initial_message_anchor = await self._latest_message_id()
            resolved_last_seen_id = initial_message_anchor
        elif isinstance(start, WatchAfter):
            normalized_id = _normalize_uuid7(start.message_id)
            resolved_last_seen_id = normalized_id
        else:
            raise InvalidMessageWindowError(f"Unsupported watch start: {start!r}")

        return MailboxWatcher(
            self,
            filter=resolved_filter,
            batch_size=batch_size,
            initial_stream_anchor=initial_stream_anchor,
            initial_last_seen_id=resolved_last_seen_id,
        )

    async def consume(
        self,
        message_ids: Iterable[str],
        *,
        consumer: str | None = None,
        strict: bool = False,
    ) -> ConsumeResult:
        """Mark messages consumed without deleting them.

        `strict=False` classifies ids into `consumed`, `already_consumed`, and
        `not_found` without raising. `strict=True` rejects unknown ids before any
        mutation, so callers can treat consume as all-or-nothing with respect to
        message existence while keeping the operation idempotent for already
        consumed messages.
        """

        ids = list(message_ids)
        if not ids:
            return ConsumeResult(consumed=[], already_consumed=[], not_found=[])

        # Keep existence checks and consumed-index mutations in one atomic Redis
        # script so compaction cannot delete a message between the two phases.
        unique_ids = list(dict.fromkeys(ids))

        consumed_at = _normalize_datetime(self._clock.now())
        raw_result = await cast(
            Awaitable[object],
            self._redis.eval(
                _CONSUME_MESSAGES_SCRIPT,
                4,
                self._keys.messages,
                self._keys.unconsumed,
                self._keys.consumed,
                self._keys.consumed_info,
                self._serializer.dump_consumed_info(
                    consumed_at,
                    consumed_by=consumer,
                ),
                "1" if strict else "0",
                *unique_ids,
            ),
        )
        consumed_raw, already_consumed_raw, not_found_raw = cast(
            list[object],
            raw_result,
        )
        consumed = _decode_string_list(consumed_raw)
        already_consumed = _decode_string_list(already_consumed_raw)
        not_found = _decode_string_list(not_found_raw)

        if strict and not_found:
            raise UnknownMessageError(not_found)

        return ConsumeResult(
            consumed=consumed,
            already_consumed=already_consumed,
            not_found=not_found,
        )

    async def compact(
        self,
        *,
        _should_continue: Callable[[], bool] | None = None,
        _delete_batch_size: int = DEFAULT_COMPACT_BATCH_SIZE,
    ) -> CompactionResult:
        """Apply the configured retention policy and clean all related indexes.

        `_should_continue` is an internal hook used by `MailboxAutoCompactor` to
        stop after the current delete batch when its Redis lock renewal fails.
        Manual callers should use the default behavior.

        Compaction removes expired messages from the message hash, timeline,
        channel indexes, consumed/unconsumed indexes, consumed-info hash, and the
        wakeup stream's age-based retention window. Retention is driven by the
        mailbox policy: old unconsumed messages are deleted too unless
        `RetentionPolicy.keep_unconsumed` is enabled, in which case those entries
        are counted as skipped and left in place.
        """

        timeline_ids = await self._zset_ids(self._keys.timeline, order="oldest_first")
        if not timeline_ids:
            stream_entries_trimmed = await self._trim_events_for_age()
            return CompactionResult(
                messages_deleted=0,
                index_entries_removed=0,
                consumed_info_removed=0,
                skipped_unconsumed=0,
                stream_entries_trimmed=stream_entries_trimmed,
            )

        timeline_messages = await self._load_messages(timeline_ids)
        if not timeline_messages:
            stream_entries_trimmed = await self._trim_events_for_age()
            return CompactionResult(
                messages_deleted=0,
                index_entries_removed=0,
                consumed_info_removed=0,
                skipped_unconsumed=0,
                stream_entries_trimmed=stream_entries_trimmed,
            )

        cutoff: datetime | None = None
        if self._retention.max_age is not None:
            cutoff = _normalize_datetime(self._clock.now()) - self._retention.max_age

        remaining_count_excess = 0
        if self._retention.max_messages is not None:
            remaining_count_excess = max(
                0,
                len(timeline_messages) - self._retention.max_messages,
            )

        messages_to_delete: list[Message] = []
        skipped_unconsumed = 0
        for message in timeline_messages:
            due_to_age = cutoff is not None and message.created_at <= cutoff
            due_to_count = remaining_count_excess > 0
            if not due_to_age and not due_to_count:
                continue
            if self._retention.keep_unconsumed and await self._is_unconsumed(
                message.id
            ):
                skipped_unconsumed += 1
                continue
            messages_to_delete.append(message)
            if due_to_count:
                remaining_count_excess -= 1

        if not messages_to_delete:
            stream_entries_trimmed = await self._trim_events_for_age()
            return CompactionResult(
                messages_deleted=0,
                index_entries_removed=0,
                consumed_info_removed=0,
                skipped_unconsumed=skipped_unconsumed,
                stream_entries_trimmed=stream_entries_trimmed,
            )

        delete_ids = [message.id for message in messages_to_delete]
        raw_consumed_infos = await cast(
            Awaitable[list[str | bytes | None]],
            self._redis.hmget(self._keys.consumed_info, delete_ids),
        )
        index_entries_removed = 0
        consumed_info_removed = 0
        deleted_messages = 0
        for batch_start in range(0, len(messages_to_delete), _delete_batch_size):
            if _should_continue is not None and not _should_continue():
                break

            batch_messages = messages_to_delete[
                batch_start : batch_start + _delete_batch_size
            ]
            batch_consumed_infos = raw_consumed_infos[
                batch_start : batch_start + _delete_batch_size
            ]
            pipeline = self._redis.pipeline(transaction=True)
            for message, raw_consumed_info in zip(
                batch_messages,
                batch_consumed_infos,
                strict=False,
            ):
                pipeline.hdel(self._keys.messages, message.id)
                pipeline.zrem(self._keys.timeline, message.id)
                pipeline.zrem(self._keys.channel_exact(message.channel), message.id)
                prefixes = iter_channel_prefixes(message.channel)
                for prefix in prefixes:
                    pipeline.zrem(self._keys.channel_prefix(prefix), message.id)
                pipeline.zrem(self._keys.unconsumed, message.id)
                pipeline.zrem(self._keys.consumed, message.id)
                pipeline.hdel(self._keys.consumed_info, message.id)

                deleted_messages += 1
                index_entries_removed += 1 + 1 + len(prefixes) + 2
                if raw_consumed_info is not None:
                    consumed_info_removed += 1

            await pipeline.execute()

            if _should_continue is not None and not _should_continue():
                break

        stream_entries_trimmed = await self._trim_events_for_age()
        return CompactionResult(
            messages_deleted=deleted_messages,
            index_entries_removed=index_entries_removed,
            consumed_info_removed=consumed_info_removed,
            skipped_unconsumed=skipped_unconsumed,
            stream_entries_trimmed=stream_entries_trimmed,
        )

    async def _trim_events_for_age(self) -> int:
        if self._retention.max_age is None:
            return 0
        cutoff = _normalize_datetime(self._clock.now()) - self._retention.max_age
        trimmed = await self._redis.xtrim(
            self._keys.events,
            minid=f"{int(cutoff.timestamp() * 1000)}-0",
            approximate=True,
        )
        return int(trimmed)

    def _candidate_scan_state(
        self,
        filter: MessageFilter,
        *,
        after_id: str | None,
        order: MessageOrder,
    ) -> _CandidateScanState:
        _ = order
        if filter.channels is None:
            if filter.consumed is True:
                return _CandidateScanState(
                    sources=[_CandidateSource(self._keys.consumed, after_id=after_id)],
                    seen_ids=set(),
                    pending_ids=[],
                )
            if filter.consumed is False:
                return _CandidateScanState(
                    sources=[
                        _CandidateSource(self._keys.unconsumed, after_id=after_id)
                    ],
                    seen_ids=set(),
                    pending_ids=[],
                )
            return _CandidateScanState(
                sources=[_CandidateSource(self._keys.timeline, after_id=after_id)],
                seen_ids=set(),
                pending_ids=[],
            )

        return _CandidateScanState(
            sources=[
                _CandidateSource(
                    self._keys.channel_exact(channel)
                    if filter.channel_match == "exact"
                    else self._keys.channel_prefix(channel),
                    after_id=after_id,
                )
                for channel in filter.iter_channels()
            ],
            seen_ids=set(),
            pending_ids=[],
        )

    async def _candidate_ids_page(
        self,
        state: _CandidateScanState,
        *,
        order: MessageOrder,
        limit: int,
    ) -> list[str]:
        emitted = state.pending_ids[:limit]
        state.pending_ids = state.pending_ids[limit:]
        remaining = limit - len(emitted)
        if remaining == 0:
            return emitted

        collected_ids: list[str] = []
        for source in state.sources:
            if source.exhausted:
                continue
            page_ids = await self._zset_ids_page(
                source,
                order=order,
                limit=remaining,
            )
            for message_id in page_ids:
                if message_id in state.seen_ids:
                    continue
                state.seen_ids.add(message_id)
                collected_ids.append(message_id)

        if not collected_ids:
            return emitted

        merged = sorted(
            [*emitted, *state.pending_ids, *collected_ids],
            reverse=order == "newest_first",
        )
        returned = merged[:limit]
        state.pending_ids = merged[limit:]
        return returned

    async def _load_messages(self, message_ids: list[str]) -> list[Message]:
        if not message_ids:
            return []

        raw_messages = await cast(
            Awaitable[list[str | bytes | None]],
            self._redis.hmget(self._keys.messages, message_ids),
        )
        raw_consumed_infos = await cast(
            Awaitable[list[str | bytes | None]],
            self._redis.hmget(self._keys.consumed_info, message_ids),
        )
        messages: list[Message] = []
        for raw_message, raw_consumed_info in zip(
            raw_messages,
            raw_consumed_infos,
            strict=False,
        ):
            if raw_message is None:
                continue
            messages.append(
                self._serializer.load_message(
                    _decode_optional_text(raw_message) or "",
                    raw_consumed_info=_decode_optional_text(raw_consumed_info),
                )
            )
        return messages

    async def _zset_ids(
        self,
        key: str,
        *,
        order: MessageOrder,
        after_id: str | None = None,
        limit: int | None = None,
    ) -> list[str]:
        if order == "oldest_first":
            values = await self._redis.zrangebylex(
                key,
                min="-" if after_id is None else f"({after_id}",
                max="+",
                start=0 if limit is not None else None,
                num=limit,
            )
        else:
            values = await self._redis.zrevrangebylex(
                key,
                max="+",
                min="-" if after_id is None else f"({after_id}",
                start=0 if limit is not None else None,
                num=limit,
            )
        return [_decode_text(value) for value in cast(list[str | bytes], values)]

    async def _zset_ids_page(
        self,
        source: _CandidateSource,
        *,
        order: MessageOrder,
        limit: int,
    ) -> list[str]:
        if order == "oldest_first":
            values = await self._redis.zrangebylex(
                source.key,
                min="-"
                if source.cursor is None and source.after_id is None
                else f"({source.cursor or source.after_id}",
                max="+",
                start=0,
                num=limit,
            )
        else:
            values = await self._redis.zrevrangebylex(
                source.key,
                max="+" if source.cursor is None else f"({source.cursor}",
                min="-" if source.after_id is None else f"({source.after_id}",
                start=0,
                num=limit,
            )

        message_ids = [_decode_text(value) for value in cast(list[str | bytes], values)]
        if not message_ids:
            source.exhausted = True
            return []
        source.cursor = message_ids[-1]
        if len(message_ids) < limit:
            source.exhausted = True
        return message_ids

    async def _is_unconsumed(self, message_id: str) -> bool:
        return (
            await cast(
                Awaitable[float | None],
                self._redis.zscore(self._keys.unconsumed, message_id),
            )
        ) is not None

    async def _latest_message_id(self) -> str | None:
        values = await self._redis.zrevrangebylex(
            self._keys.timeline,
            max="+",
            min="-",
            start=0,
            num=1,
        )
        if not values:
            return None
        return _decode_text(cast(str | bytes, values[0]))

    async def _latest_stream_id(self) -> str:
        entries = _coerce_stream_entries(
            await self._redis.xrevrange(self._keys.events, count=1)
        )
        if not entries:
            return "0-0"
        return entries[0][0]

    async def _wait_for_event(self, stream_id: str) -> str:
        entries = _coerce_xread(
            await self._redis.xread(
                {self._keys.events: stream_id},
                count=1,
                block=0,
            )
        )
        if not entries:
            return stream_id
        _, stream_entries = entries[0]
        if not stream_entries:
            return stream_id
        return stream_entries[-1][0]

    def _normalize_channel(self, channel: str, *, field_name: str) -> str:
        try:
            return validate_channel_path(channel, field_name=field_name)
        except (TypeError, ValueError) as exc:
            raise InvalidChannelError(str(exc)) from exc

    def _normalize_after_id(self, after_id: str | None) -> str | None:
        if after_id is None:
            return None
        return _normalize_uuid7(after_id)


class MailboxWriter:
    """Write-only mailbox facade for producers.

    Producers only need append access. Hiding query/watch/consume keeps their
    dependency surface narrow and makes producer supervision easier to reason
    about.
    """

    def __init__(self, mailbox: RedisMailbox) -> None:
        self._mailbox = mailbox

    async def put(
        self,
        channel: str,
        payload: JsonValue,
        *,
        producer: str | None = None,
    ) -> Message:
        return await self._mailbox.put(channel, payload, producer=producer)

    async def put_many(
        self,
        items: Iterable[MessageInput],
        *,
        producer: str | None = None,
    ) -> list[Message]:
        return await self._mailbox.put_many(items, producer=producer)


class MailboxWatcher:
    """Independent watch session over a mailbox.

    A watcher tracks its own message cursor and Redis stream wakeup cursor. It is
    safe to create many watchers from the same mailbox because each watcher keeps
    isolated read state and can be closed without affecting the mailbox.
    """

    def __init__(
        self,
        mailbox: RedisMailbox,
        *,
        filter: MessageFilter,
        batch_size: int,
        initial_stream_anchor: str = "0-0",
        initial_last_seen_id: str | None = None,
    ) -> None:
        if batch_size <= 0:
            raise InvalidMessageWindowError("batch_size must be > 0")
        self._mailbox = mailbox
        self._filter = filter
        self._batch_size = batch_size
        self._initial_stream_anchor = initial_stream_anchor
        self._initial_last_seen_id = initial_last_seen_id
        self._initialized = False
        self._closed = False
        self._last_seen_id: str | None = None
        self._stream_cursor = "0-0"
        self._wait_task: asyncio.Task[str] | None = None

    async def __aenter__(self) -> MailboxWatcher:
        await self._initialize()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    def __aiter__(self) -> MailboxWatcher:
        return self

    async def __anext__(self) -> list[Message]:
        try:
            return await self.next()
        except WatchClosedError as exc:
            raise StopAsyncIteration from exc

    async def try_next(self) -> list[Message]:
        """Return the next matching batch without waiting for future messages."""

        self._ensure_open()
        await self._initialize()
        messages = await self._mailbox.get(
            self._filter,
            after_id=self._last_seen_id,
            limit=self._batch_size,
            order="oldest_first",
        )
        if not messages:
            return []

        self._last_seen_id = messages[-1].id
        return messages

    async def next(self) -> list[Message]:
        """Block until at least one matching message is available.

        `next()` retries a small number of recoverable Redis connection failures
        from the blocking watch path and the immediate follow-up reads while
        preserving the watcher's current cursor. Non-connection domain errors such
        as invalid filters or closed-watcher misuse are surfaced immediately.
        """

        self._ensure_open()
        await self._initialize()
        retry_index = 0
        while True:
            try:
                messages = await self.try_next()
                if messages:
                    return messages
                await self._block_for_wakeup()
                retry_index = 0
            except asyncio.CancelledError:
                raise
            except WatchClosedError:
                raise
            except Exception as exc:
                if not _is_recoverable_watcher_error(exc):
                    raise
                if retry_index >= len(WATCHER_RETRY_DELAYS_SECONDS):
                    raise
                delay = WATCHER_RETRY_DELAYS_SECONDS[retry_index]
                retry_index += 1
                await asyncio.sleep(delay)

    async def close(self) -> None:
        """Close the watcher and cancel any pending blocking read."""

        if self._closed:
            return
        self._closed = True
        if self._wait_task is not None:
            self._wait_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._wait_task
            self._wait_task = None

    async def _initialize(self) -> None:
        if self._initialized:
            return
        self._stream_cursor = self._initial_stream_anchor
        self._last_seen_id = self._initial_last_seen_id
        self._initialized = True

    async def _block_for_wakeup(self) -> None:
        self._ensure_open()
        self._wait_task = asyncio.create_task(
            self._mailbox._wait_for_event(self._stream_cursor)
        )
        try:
            self._stream_cursor = await self._wait_task
        except asyncio.CancelledError:
            if self._closed:
                raise WatchClosedError("watcher is closed") from None
            raise
        finally:
            self._wait_task = None

    def _ensure_open(self) -> None:
        if self._closed:
            raise WatchClosedError("watcher is closed")


class MailboxProducerSupervisor:
    """Supervises long-running async producers and restarts them on failure.

    Each registered producer runs in its own supervision loop. A producer that
    returns normally or raises unexpectedly is logged and restarted with bounded
    exponential backoff unless the supervisor is shutting down. `stop()` /
    `__aexit__` cancel restart intent first, then wait for the current producer
    tasks to exit, so shutdown never schedules a fresh restart.
    """

    def __init__(
        self,
        writer: MailboxWriter,
        *,
        restart_policy: RestartPolicy | None = None,
        logger: Logger | None = None,
    ) -> None:
        self._writer = writer
        self._restart_policy = restart_policy or RestartPolicy()
        self._logger = logger or getLogger(__name__)
        self._producers: dict[str, tuple[MailboxProducer, _ProducerRuntime]] = {}
        self._stop_event = asyncio.Event()
        self._closed = False

    async def __aenter__(self) -> MailboxProducerSupervisor:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    def register_producer(
        self,
        name: str,
        producer: MailboxProducer,
    ) -> ProducerHandle:
        """Register a named producer for later start."""

        if self._closed:
            raise ProducerSupervisorClosedError("producer supervisor is closed")
        if name in self._producers:
            raise ProducerAlreadyRegisteredError(
                f"Producer {name!r} is already registered"
            )
        self._producers[name] = (producer, _ProducerRuntime(name=name))
        return ProducerHandle(name=name)

    async def start(self, names: Iterable[str] | None = None) -> None:
        """Start all or selected producer supervisor loops."""

        if self._closed:
            raise ProducerSupervisorClosedError("producer supervisor is closed")
        selected_names = list(names) if names is not None else list(self._producers)
        for name in selected_names:
            producer, runtime = self._producers[name]
            if runtime.task is not None and not runtime.task.done():
                continue
            runtime.task = asyncio.create_task(
                self._run_supervisor_loop(name, producer, runtime)
            )

    async def stop(self, timeout: float | None = None) -> None:
        """Request shutdown, cancel producers if needed, and disable restarts."""

        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        tasks: list[asyncio.Task[None]] = []
        for _, runtime in self._producers.values():
            runtime.state = "stopping"
            if runtime.cancellation_token is not None:
                runtime.cancellation_token.cancel()
            if runtime.task is not None:
                tasks.append(runtime.task)

        if not tasks:
            return

        gather = asyncio.gather(*tasks, return_exceptions=True)
        try:
            if timeout is None:
                await gather
            else:
                await asyncio.wait_for(gather, timeout=timeout)
        except TimeoutError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for _, runtime in self._producers.values():
                runtime.state = "stopped"

    def status(self, name: str) -> ProducerStatus:
        """Return a snapshot of one producer's current status."""

        _, runtime = self._producers[name]
        return runtime.snapshot()

    async def _run_supervisor_loop(
        self,
        name: str,
        producer: MailboxProducer,
        runtime: _ProducerRuntime,
    ) -> None:
        while not self._stop_event.is_set():
            runtime.state = "running"
            runtime.cancellation_token = CancellationToken()
            runtime.last_started_at = datetime.now(UTC)

            try:
                await producer(self._writer, runtime.cancellation_token)
                if self._stop_event.is_set():
                    break
                runtime.last_error = "producer returned"
                self._logger.warning(
                    "Producer %s returned unexpectedly; restarting",
                    name,
                )
            except asyncio.CancelledError:
                if self._stop_event.is_set():
                    raise
                runtime.last_error = "producer task cancelled"
                self._logger.warning(
                    "Producer %s was cancelled unexpectedly; restarting",
                    name,
                )
            except Exception as exc:
                runtime.last_error = f"{type(exc).__name__}: {exc}"
                self._logger.exception(
                    "Producer %s failed; restarting",
                    name,
                    exc_info=exc,
                )

            if self._stop_event.is_set():
                break

            runtime.restart_count += 1
            runtime.consecutive_failures += 1
            runtime.state = "backoff"
            delay = self._restart_policy.delay_for_restart(runtime.consecutive_failures)
            self._logger.warning(
                "Producer %s restarting in %.2fs (restart #%s)",
                name,
                delay,
                runtime.restart_count,
            )
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
            except TimeoutError:
                continue

        runtime.state = "stopped"


class MailboxAutoCompactor:
    """Runs `mailbox.compact()` periodically in a separate lifecycle component.

    The compactor sleeps for `interval` plus optional jitter, then runs one
    compaction round. When `use_redis_lock=True`, it acquires a namespace-level
    Redis lock before compacting and renews that lock periodically. If lock
    renewal is lost or fails, the current compaction round is allowed to finish
    only its current delete batch before stopping, which avoids overlapping full
    rounds across processes while keeping shutdown semantics predictable.
    """

    def __init__(
        self,
        mailbox: RedisMailbox,
        *,
        interval: timedelta = DEFAULT_COMPACT_INTERVAL,
        use_redis_lock: bool = True,
        lock_ttl: timedelta = DEFAULT_COMPACT_LOCK_TTL,
        lock_renew_interval: timedelta = DEFAULT_COMPACT_LOCK_RENEW_INTERVAL,
        jitter_ratio: float = 0.1,
        logger: Logger | None = None,
    ) -> None:
        self._mailbox = mailbox
        self._interval = interval
        self._use_redis_lock = use_redis_lock
        self._lock_ttl = lock_ttl
        self._lock_renew_interval = lock_renew_interval
        self._jitter_ratio = jitter_ratio
        self._logger = logger or getLogger(__name__)
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._closed = False

    async def __aenter__(self) -> MailboxAutoCompactor:
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.stop()

    async def start(self) -> None:
        """Start the periodic compaction task if it is not already running."""

        if self._closed:
            raise AutoCompactorClosedError("auto-compactor is closed")
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Stop periodic compaction and prevent future scheduling."""

        if self._closed:
            return
        self._closed = True
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._sleep_seconds_with_jitter(),
                )
                break
            except TimeoutError:
                pass

            try:
                if self._use_redis_lock:
                    lock_token = uuid.uuid4().hex
                    if not await self._try_acquire_lock(lock_token):
                        continue
                    lock_lost = asyncio.Event()
                    renew_task = asyncio.create_task(
                        self._renew_lock(lock_token, lock_lost)
                    )
                    try:
                        await self._mailbox.compact(
                            _should_continue=lambda lock_lost=lock_lost: (
                                not lock_lost.is_set()
                            ),
                        )
                    finally:
                        renew_task.cancel()
                        await asyncio.gather(renew_task, return_exceptions=True)
                        await self._release_lock(lock_token)
                else:
                    await self._mailbox.compact()
            except asyncio.CancelledError:
                raise
            except Exception:
                self._logger.exception("Automatic mailbox compaction failed")

    def _sleep_seconds_with_jitter(self) -> float:
        base = self._interval.total_seconds()
        jitter = base * self._jitter_ratio
        if jitter == 0:
            return base
        return max(0.0, base + random.uniform(-jitter, jitter))

    async def _try_acquire_lock(self, token: str) -> bool:
        result = await self._mailbox._redis.set(
            self._mailbox._keys.compact_lock,
            token,
            nx=True,
            px=int(self._lock_ttl.total_seconds() * 1000),
        )
        return _as_bool(result)

    async def _renew_lock(self, token: str, lock_lost: asyncio.Event) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._lock_renew_interval.total_seconds(),
                )
                return
            except TimeoutError:
                pass

            try:
                renewed = await cast(
                    Awaitable[object],
                    self._mailbox._redis.eval(
                        _RENEW_COMPACT_LOCK_SCRIPT,
                        1,
                        self._mailbox._keys.compact_lock,
                        token,
                        int(self._lock_ttl.total_seconds() * 1000),
                    ),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                lock_lost.set()
                self._logger.exception("Mailbox compaction lock renewal failed")
                return
            if not _as_bool(renewed):
                lock_lost.set()
                return

    async def _release_lock(self, token: str) -> None:
        await cast(
            Awaitable[object],
            self._mailbox._redis.eval(
                _RELEASE_COMPACT_LOCK_SCRIPT,
                1,
                self._mailbox._keys.compact_lock,
                token,
            ),
        )


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _normalize_uuid7(value: str) -> str:
    try:
        normalized = uuid.UUID(value)
    except ValueError as exc:
        raise InvalidMessageWindowError(f"Invalid UUID: {value!r}") from exc
    if normalized.version != 7:
        raise InvalidMessageWindowError(f"Expected UUIDv7: {value!r}")
    return str(normalized)


def _decode_text(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return value


def _decode_optional_text(value: str | bytes | None) -> str | None:
    if value is None:
        return None
    return _decode_text(value)


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.upper() == "OK"
    if isinstance(value, int):
        return value != 0
    return bool(value)


def _is_recoverable_watcher_error(exc: Exception) -> bool:
    """Return whether `MailboxWatcher.next()` should retry an exception."""

    return isinstance(exc, (RedisConnectionError, RedisTimeoutError))


def _query_page_size(remaining: int | None) -> int:
    if remaining is None:
        return DEFAULT_QUERY_PAGE_SIZE
    return max(1, min(DEFAULT_QUERY_PAGE_SIZE, remaining))


def _decode_string_list(value: object) -> list[str]:
    return [_decode_text(cast(str | bytes, item)) for item in cast(list[object], value)]


def _coerce_stream_entries(value: object) -> list[tuple[str, dict[str, str]]]:
    entries: list[tuple[str, dict[str, str]]] = []
    for raw_entry in cast(list[object], value):
        raw_id, raw_fields = cast(tuple[object, object], raw_entry)
        entries.append(
            (
                _decode_text(cast(str | bytes, raw_id)),
                _coerce_stream_fields(raw_fields),
            )
        )
    return entries


def _coerce_xread(value: object) -> list[tuple[str, list[tuple[str, dict[str, str]]]]]:
    streams: list[tuple[str, list[tuple[str, dict[str, str]]]]] = []
    for raw_stream in cast(list[object], value):
        raw_name, raw_entries = cast(tuple[object, object], raw_stream)
        streams.append(
            (
                _decode_text(cast(str | bytes, raw_name)),
                _coerce_stream_entries(raw_entries),
            )
        )
    return streams


def _coerce_stream_fields(value: object) -> dict[str, str]:
    raw_fields = cast(dict[object, object], value)
    return {
        _decode_text(cast(str | bytes, key)): _decode_text(
            cast(str | bytes, field_value)
        )
        for key, field_value in raw_fields.items()
    }

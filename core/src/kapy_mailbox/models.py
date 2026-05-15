"""Shared value objects for the Redis-backed mailbox.

These models hold user-facing mailbox contracts and lightweight lifecycle state.
Validation stays close to the data shape so `RedisMailbox`, watchers, and
supervisor helpers can share one set of invariants.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from k.agent.channels import channel_has_prefix, validate_channel_path
from kapy_mailbox.exceptions import InvalidMessageFilterError

type JsonValue = (
    None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]
)
type MessageOrder = Literal["oldest_first", "newest_first"]
type ChannelMatch = Literal["subtree", "exact"]
type ProducerState = Literal[
    "registered",
    "running",
    "backoff",
    "stopping",
    "stopped",
]


def _normalize_datetime(value: datetime) -> datetime:
    """Return an aware UTC datetime.

    The mailbox stores and compares UTC timestamps. Naive datetimes are treated
    as UTC so tests can inject compact fixtures without extra timezone setup.
    """

    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


@dataclass(slots=True, frozen=True)
class Message:
    """Immutable mailbox message snapshot."""

    id: str
    channel: str
    payload: JsonValue
    created_at: datetime
    producer: str | None = None
    consumed_at: datetime | None = None
    consumed_by: str | None = None


@dataclass(slots=True, frozen=True)
class MessageInput:
    """Batch input item for `put_many`."""

    channel: str
    payload: JsonValue
    producer: str | None = None


@dataclass(slots=True, frozen=True)
class MessageFilter:
    """Composable message-selection predicates shared by `get()` and `watch()`.

    Channel selection is expressed through `channel` and/or `channels`. The two
    forms are merged, validated, and treated as a set of normalized channel
    roots. By default `channel_match="subtree"`, so a filter for
    `telegram/chat/1` matches that exact channel and everything below it;
    `channel_match="exact"` restricts results to the exact channel ids.

    `consumed` controls consume-state selection: `None` means both consumed and
    unconsumed messages, `False` means only currently unconsumed messages, and
    `True` means only currently consumed messages. `since` and `until` bound the
    accepted `created_at` timestamps inclusively after UTC normalization.
    `producer` restricts matches to messages written with that producer label.
    """

    channel: str | None = None
    channels: frozenset[str] | None = None
    channel_match: ChannelMatch = "subtree"
    consumed: bool | None = None
    since: datetime | None = None
    until: datetime | None = None
    producer: str | None = None

    def __post_init__(self) -> None:
        normalized_channels: set[str] = set()
        if self.channel is not None:
            normalized_channels.add(
                self._normalize_channel(self.channel, field_name="channel")
            )
        if self.channels is not None:
            if not self.channels:
                raise InvalidMessageFilterError("channels must not be empty")
            for index, value in enumerate(sorted(self.channels)):
                normalized_channels.add(
                    self._normalize_channel(value, field_name=f"channels[{index}]")
                )

        if self.channel_match not in {"subtree", "exact"}:
            raise InvalidMessageFilterError(
                f"Unsupported channel_match: {self.channel_match!r}"
            )

        if self.since is not None:
            object.__setattr__(self, "since", _normalize_datetime(self.since))
        if self.until is not None:
            object.__setattr__(self, "until", _normalize_datetime(self.until))
        if (
            self.since is not None
            and self.until is not None
            and self.since > self.until
        ):
            raise InvalidMessageFilterError("since must be <= until")

        object.__setattr__(
            self,
            "channels",
            None if not normalized_channels else frozenset(normalized_channels),
        )
        if self.channel is not None:
            object.__setattr__(
                self,
                "channel",
                self._normalize_channel(self.channel, field_name="channel"),
            )

    @staticmethod
    def _normalize_channel(value: str, *, field_name: str) -> str:
        try:
            return validate_channel_path(value, field_name=field_name)
        except (TypeError, ValueError) as exc:
            raise InvalidMessageFilterError(str(exc)) from exc

    def iter_channels(self) -> tuple[str, ...]:
        """Return normalized channels as a stable tuple."""

        if self.channels is None:
            return ()
        return tuple(sorted(self.channels))

    def matches(self, message: Message) -> bool:
        """Return whether a message matches this filter snapshot."""

        if self.channels is not None:
            if self.channel_match == "exact":
                if message.channel not in self.channels:
                    return False
            else:
                if not any(
                    channel_has_prefix(channel=message.channel, prefix=channel)
                    for channel in self.channels
                ):
                    return False

        if self.consumed is not None:
            is_consumed = message.consumed_at is not None
            if is_consumed != self.consumed:
                return False

        if (
            self.since is not None
            and _normalize_datetime(message.created_at) < self.since
        ):
            return False
        if (
            self.until is not None
            and _normalize_datetime(message.created_at) > self.until
        ):
            return False
        if self.producer is not None and message.producer != self.producer:
            return False
        return True


@dataclass(slots=True, frozen=True)
class ConsumeResult:
    """Result of a consume call grouped by final classification."""

    consumed: list[str]
    already_consumed: list[str]
    not_found: list[str]


@dataclass(slots=True, frozen=True)
class RetentionPolicy:
    """Mailbox retention settings used by manual and automatic compaction."""

    max_messages: int | None = None
    max_age: timedelta | None = timedelta(days=3)
    keep_unconsumed: bool = False

    def __post_init__(self) -> None:
        if self.max_messages is not None and self.max_messages < 0:
            raise ValueError("max_messages must be >= 0")
        if self.max_age is not None and self.max_age < timedelta(0):
            raise ValueError("max_age must be >= 0")


@dataclass(slots=True, frozen=True)
class CompactionResult:
    """Summary returned by `compact()`."""

    messages_deleted: int
    index_entries_removed: int
    consumed_info_removed: int
    skipped_unconsumed: int
    stream_entries_trimmed: int


@dataclass(slots=True, frozen=True)
class WatchAfter:
    """Tagged watch start value that resumes strictly after a message id."""

    message_id: str


class WatchStart:
    """Factory namespace for watcher start values.

    The mailbox intentionally keeps the public surface small: callers use the
    plain strings `"new"` and `"oldest"`, or `WatchStart.after(id)` for resume.
    """

    NEW: Literal["new"] = "new"
    OLDEST: Literal["oldest"] = "oldest"

    @staticmethod
    def after(message_id: str) -> WatchAfter:
        return WatchAfter(message_id=message_id)


type WatchStartValue = Literal["new", "oldest"] | WatchAfter


class CancellationToken:
    """Simple producer-facing cancellation token.

    Producers should cooperate by checking `is_cancelled()` or awaiting
    `wait_cancelled()` in long-running loops instead of relying on task
    cancellation alone.
    """

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    async def wait_cancelled(self) -> None:
        await self._event.wait()


@dataclass(slots=True, frozen=True)
class RestartPolicy:
    """Bounded exponential backoff settings for producer restarts."""

    initial_delay_seconds: float = 1.0
    max_delay_seconds: float = 60.0
    multiplier: float = 2.0
    jitter_ratio: float = 0.1

    def __post_init__(self) -> None:
        if self.initial_delay_seconds <= 0:
            raise ValueError("initial_delay_seconds must be > 0")
        if self.max_delay_seconds < self.initial_delay_seconds:
            raise ValueError("max_delay_seconds must be >= initial_delay_seconds")
        if self.multiplier < 1:
            raise ValueError("multiplier must be >= 1")
        if not 0 <= self.jitter_ratio <= 1:
            raise ValueError("jitter_ratio must be between 0 and 1")

    def delay_for_restart(self, failures: int) -> float:
        """Return a bounded, jittered restart delay for a failure count."""

        exponent = max(0, failures - 1)
        delay = min(
            self.max_delay_seconds,
            self.initial_delay_seconds * (self.multiplier**exponent),
        )
        jitter = delay * self.jitter_ratio
        if jitter == 0:
            return delay
        return max(0.0, delay + random.uniform(-jitter, jitter))


@dataclass(slots=True, frozen=True)
class ProducerStatus:
    """Snapshot of one supervised producer."""

    name: str
    state: ProducerState
    restart_count: int
    last_error: str | None = None
    last_started_at: datetime | None = None


@dataclass(slots=True, frozen=True)
class ProducerHandle:
    """Opaque registration handle for a producer."""

    name: str


@dataclass(slots=True)
class _ProducerRuntime:
    """Mutable internal state tracked by the producer supervisor."""

    name: str
    state: ProducerState = "registered"
    restart_count: int = 0
    last_error: str | None = None
    last_started_at: datetime | None = None
    cancellation_token: CancellationToken | None = None
    task: asyncio.Task[None] | None = None
    consecutive_failures: int = 0

    def snapshot(self) -> ProducerStatus:
        return ProducerStatus(
            name=self.name,
            state=self.state,
            restart_count=self.restart_count,
            last_error=self.last_error,
            last_started_at=self.last_started_at,
        )

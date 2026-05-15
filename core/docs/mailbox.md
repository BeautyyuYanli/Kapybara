# Mailbox Guide

## Overview

`kapy_mailbox` provides a Redis-backed mailbox layer for agent-style runtimes.

It is designed for workloads where multiple external producers feed messages into a
shared inbox, while one or more agent loops need to:

- append messages
- query recent or unconsumed history
- wait for future messages
- mark handled messages as consumed
- compact old history on a retention policy

The package is intentionally **not** a consumer-group queue or a general-purpose
broker replacement. It is closer to a small event-log plus inbox abstraction with
Redis-friendly read and recovery semantics.

Current implementation package:

- `core/src/kapy_mailbox/`

## Design goals

The mailbox implementation follows these principles:

1. **RedisMailbox is lightweight**
   - it wraps an injected `redis.asyncio.Redis` client
   - it does not own the Redis connection lifecycle
   - it does not start background tasks by itself

2. **Lifecycle objects are separate**
   - watcher: `MailboxWatcher`
   - producer supervisor: `MailboxProducerSupervisor`
   - auto-compactor: `MailboxAutoCompactor`

3. **Ordering uses UUIDv7**
   - every stored message gets a mailbox-assigned UUIDv7 id
   - that id is both the public message id and the mailbox ordering cursor

4. **Watcher semantics are arrival-based**
   - watch observes message arrival
   - it does not observe consume-state transitions
   - therefore `watch(..., consumed=True)` is invalid

5. **Resume cursors are pure cursors**
   - `WatchStart.after(id)` only requires a syntactically valid UUIDv7 string
   - the referenced message does not need to exist in Redis
   - there is no fallback or warning for missing/compacted ids

## Public API surface

The public surface is intentionally small.

### Main types

- `RedisMailbox`
- `MailboxWatcher`
- `MailboxWriter`
- `MailboxProducerSupervisor`
- `MailboxAutoCompactor`

### Models

- `Message`
- `MessageInput`
- `MessageFilter`
- `ConsumeResult`
- `CompactionResult`
- `RetentionPolicy`
- `RestartPolicy`
- `WatchStart`

### Exceptions

- `InvalidChannelError`
- `PayloadSerializationError`
- `InvalidMessageFilterError`
- `InvalidMessageWindowError`
- `UnknownMessageError`
- `WatchClosedError`
- `ProducerAlreadyRegisteredError`
- `ProducerSupervisorClosedError`
- `AutoCompactorClosedError`

## Dependencies

Runtime dependencies used by the mailbox implementation:

- `redis>=7.4.0`
- `pydantic>=2.12.5`
- `uuid6>=2025.0.1`

Test dependency used by mailbox tests:

- `fakeredis[lua]>=2.35.1`

## Quick start

### Basic mailbox usage

```python
from redis.asyncio import Redis

from kapy_mailbox import MessageFilter, RedisMailbox

redis = Redis.from_url("redis://localhost:6379")
mailbox = RedisMailbox(redis, namespace="agent-main")

await mailbox.put("telegram/chat/1", {"text": "hello"}, producer="telegram")

messages = await mailbox.get(
    MessageFilter(consumed=False),
    order="oldest_first",
)

await mailbox.consume([message.id for message in messages], consumer="agent-loop")
```

### Watching future messages

```python
from kapy_mailbox import MessageFilter

async with (await mailbox.watch(MessageFilter(consumed=False), start="new")) as watcher:
    messages = await watcher.next()
```

### Resuming from a saved cursor

```python
from kapy_mailbox import MessageFilter, WatchStart

async with (
    await mailbox.watch(
        MessageFilter(consumed=False),
        start=WatchStart.after(last_seen_id),
    )
) as watcher:
    messages = await watcher.try_next()
```

`last_seen_id` only needs to be a valid UUIDv7 string. It can refer to a message
that no longer exists.

## Message model

`Message` snapshots contain:

- `id: str`
- `channel: str`
- `payload: JsonValue`
- `created_at: datetime`
- `producer: str | None`
- `consumed_at: datetime | None`
- `consumed_by: str | None`

The payload boundary is JSON-compatible data validated and serialized by
Pydantic-backed serializer logic.

## Channels and filtering

Mailbox channels reuse the existing hierarchical channel conventions from the `k`
package:

- slash-separated hierarchy
- no empty segments
- no leading/trailing slash

Examples:

- `telegram/chat/1`
- `telegram/chat/1/thread/10`

`MessageFilter` supports:

- `channel`
- `channels`
- `channel_match="subtree" | "exact"`
- `consumed=None | False | True`
- `since`
- `until`
- `producer`

Default channel matching is subtree-based.

### Examples

Recent messages in one subtree:

```python
messages = await mailbox.get(
    MessageFilter(channel="telegram/chat/1"),
    limit=20,
    order="newest_first",
)
```

Exact-channel only:

```python
messages = await mailbox.get(
    MessageFilter(channel="telegram/chat/1", channel_match="exact"),
)
```

All consumed messages:

```python
messages = await mailbox.get(MessageFilter(consumed=True))
```

## Read semantics

### `get()`

Signature:

```python
await mailbox.get(filter=None, after_id=None, limit=100, order="oldest_first")
```

Important rules:

- default `limit=100`
- pass `limit=None` explicitly for an unbounded scan
- `after_id` is a UUIDv7 ordering cursor
- results are immutable snapshots

### `WatchStart`

Watcher start modes:

- `"new"`
  - start strictly after the mailbox latest id at watch-creation time
- `"oldest"`
  - replay historical matches first
- `WatchStart.after(id)`
  - pure UUIDv7 cursor semantics

## Watcher behavior

### `watch()` is async

`RedisMailbox.watch(...)` is async because `start="new"` needs to anchor against
Redis state before the watcher is returned.

### `try_next()`

- non-blocking
- returns `[]` if no matching message is currently available
- **does not** auto-retry connection failures

### `next()`

- blocks until at least one matching message is available
- retries a small number of recoverable Redis connection failures
- preserves the watcher cursor across retries

Current default retry policy:

- 3 retries
- backoff: `0.1s`, `0.5s`, `1.0s`

Retry is intended for recoverable Redis connection/timeout class failures, not for
domain errors such as:

- closed watcher use
- invalid filter
- invalid UUID window arguments

### Unsupported watcher filter

This is intentionally invalid:

```python
await mailbox.watch(MessageFilter(consumed=True), start="new")
```

Reason: watch observes **message arrival**, not later consume-state transitions.
Callers that need consumed history should use `get(MessageFilter(consumed=True), ...)`.

## Consumption semantics

`consume()` is explicit and idempotent.

```python
result = await mailbox.consume(ids, consumer="agent-loop", strict=False)
```

The returned `ConsumeResult` classifies ids into:

- `consumed`
- `already_consumed`
- `not_found`

With `strict=True`, unknown ids raise `UnknownMessageError` instead of being left
in `not_found`.

`consume()` does **not** delete messages.

## Producer-facing writer

`MailboxWriter` is a write-only facade intended for supervised producers.

It exposes:

- `put(...)`
- `put_many(...)`

It intentionally does not expose `get`, `watch`, `consume`, or `compact`.

## Producer supervision

`MailboxProducerSupervisor` runs producers in restartable supervision loops.

### Behavior

- producers are registered by name
- `start()` launches supervisor tasks
- `stop()` prevents further restarts and waits for shutdown
- producer return or exception is treated as a failure/restart condition
- restart uses bounded exponential backoff

Example:

```python
from kapy_mailbox import MailboxProducerSupervisor

supervisor = MailboxProducerSupervisor(mailbox.writer())
supervisor.register_producer("telegram", telegram_producer)

async with supervisor:
    ...
```

## Retention and compaction

Retention policy defaults:

- `max_messages=None`
- `max_age=timedelta(days=3)`
- `keep_unconsumed=False`

This means old unconsumed messages may be compacted unless explicitly protected.

### Manual compaction

```python
result = await mailbox.compact()
```

Compaction removes retained-out messages from:

- message storage
- timeline index
- channel indexes
- unconsumed index
- consumed index
- consumed-info hash
- aged event-stream entries

### Automatic compaction

`MailboxAutoCompactor` periodically calls `mailbox.compact()`.

Default settings:

- interval: 15 minutes
- jitter: ±10%
- optional Redis lock enabled
- lock TTL: 5 minutes
- lock renew interval: 60 seconds

If lock renewal is lost, the current compaction round finishes only the current
delete batch and then stops that round.

Example:

```python
from datetime import timedelta
from kapy_mailbox import MailboxAutoCompactor

async with MailboxAutoCompactor(mailbox, interval=timedelta(minutes=1)):
    ...
```

## Concurrency model

### Multiple mailbox instances

Multiple `RedisMailbox` instances may point at the same Redis namespace.

This is normal for:

- multiple watchers
- separate producer and consumer processes
- separate UI / debug readers

### Multiple watchers

Multiple watchers on the same namespace are supported.

They are independent read sessions:

- each keeps its own `last_seen_id`
- each keeps its own stream wakeup cursor
- they do not claim messages exclusively

This is broadcast-style observation, not consumer-group partitioning.

## Redis client lifecycle and reconnect behavior

`RedisMailbox` does not own the Redis client lifecycle.

- caller creates the `redis.asyncio.Redis` client
- caller closes it
- mailbox methods surface Redis errors rather than silently swallowing them

Reconnect behavior today:

- ordinary Redis client commands rely on redis-py connection behavior
- `MailboxWatcher.next()` adds mailbox-level retry for recoverable connection failures
- `MailboxWatcher.try_next()` does not retry
- if retries are exhausted, the last Redis error is raised to the caller

## Main-flow validation

The mailbox main flow was manually validated against a real Redis instance running
in Docker, covering:

- `put`
- `put_many`
- `writer.put`
- `get`
- `watch(start="new")`
- `watch(start=WatchStart.after(...))`
- `consume`
- `compact`
- `MailboxProducerSupervisor`
- `MailboxAutoCompactor`

This validation was done with a temporary Redis container rather than only fake
Redis tests.

## Suggested usage boundaries

Use this mailbox when you need:

- agent inbox semantics
- recent history lookups
- Redis-backed watcher resume via UUIDv7 cursor
- explicit consume state
- simple supervised producers

Do not treat it as:

- a consumer-group queue
- a distributed exactly-once system
- a replacement for Kafka/RabbitMQ-style broker semantics

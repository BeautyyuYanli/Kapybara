# Mailbox Guide

## Overview

`kapy_mailbox` provides a Redis-backed mailbox layer for agent-style runtimes.

It is designed for workloads where multiple external producers feed messages into a
shared inbox, while one or more agent loops need to:

- append messages
- query recent or unconsumed history
- mark handled messages as consumed
- compact old history on a retention policy

The package is intentionally **not** a consumer-group queue or a broker
replacement. It is closer to a small event-log plus inbox abstraction with
Redis-friendly query and recovery semantics.

Current implementation package:

- `core/src/kapy_mailbox/`

## Design goals

The mailbox implementation follows these principles:

1. **RedisMailbox is lightweight**
   - it wraps an injected `redis.asyncio.Redis` client
   - it does not own the Redis connection lifecycle
   - it does not start background tasks by itself

2. **Lifecycle objects are separate**
   - producer supervisor: `MailboxProducerSupervisor`
   - auto-compactor: `MailboxAutoCompactor`

3. **Ordering uses UUIDv7**
   - every stored message gets a mailbox-assigned UUIDv7 id
   - that id is both the public message id and the mailbox ordering cursor

4. **Incremental reads use pure cursors**
   - `get(..., after_id=id)` only requires a syntactically valid UUIDv7 string
   - the referenced message does not need to exist in Redis
   - there is no fallback for missing or compacted ids

5. **Consume state is explicit**
   - `consume()` updates consume metadata
   - reads observe current consume state through `MessageFilter(consumed=...)`

## Public API surface

### Main types

- `RedisMailbox`
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

### Exceptions

- `InvalidChannelError`
- `PayloadSerializationError`
- `InvalidMessageFilterError`
- `InvalidMessageWindowError`
- `UnknownMessageError`
- `ProducerAlreadyRegisteredError`
- `ProducerSupervisorClosedError`
- `AutoCompactorClosedError`

## Quick start

```python
from kapy_mailbox import MessageFilter, RedisMailbox
from redis.asyncio import Redis

redis = Redis.from_url("redis://localhost:6379")
mailbox = RedisMailbox(redis, namespace="agent-main")

await mailbox.put("telegram/chat/1", {"text": "hello"}, producer="telegram")

messages = await mailbox.get(
    MessageFilter(consumed=False),
    order="oldest_first",
)

await mailbox.consume([message.id for message in messages], consumer="agent-loop")
```

## Incremental polling

`get()` supports resumable polling via `after_id`.

```python
batch = await mailbox.get(after_id=last_seen_id, limit=50)
if batch:
    last_seen_id = batch[-1].id
```

Common migration patterns from the removed watch API:

- replay history: start with `after_id = None`
- future-only polling: seed `after_id` from the current newest message before the
  loop

```python
# Replay from current history.
after_id = None

# Or skip backlog and only poll future arrivals.
latest = await mailbox.get(limit=1, order="newest_first")
after_id = latest[0].id if latest else None

while True:
    batch = await mailbox.get(after_id=after_id, limit=50)
    if batch:
        after_id = batch[-1].id
        ...
```

`after_id` only needs to be a valid UUIDv7 string. It can refer to a message that
no longer exists.

## Message model

`Message` snapshots contain:

- `id: str`
- `channel: str`
- `payload: JsonValue`
- `created_at: datetime`
- `producer: str | None`
- `consumed_at: datetime | None`
- `consumed_by: str | None`

The payload boundary is JSON-compatible data validated and serialized by the
configured serializer.

## Channels and filtering

Mailbox channels reuse the existing slash-separated channel validation
conventions from the `k` package:

- slash-separated hierarchy
- no empty segments
- no leading/trailing slash

Examples:

- `telegram/chat/1`
- `telegram/chat/1/thread/10`

`MessageFilter` supports:

- `channel`
- `consumed=None | False | True`
- `since`
- `until`
- `producer`

Mailbox filtering treats `channel` as one exact identifier, not a subtree root.
That means `telegram/chat/1` and `telegram/chat/1/thread/10` are distinct
mailbox channels for query purposes even though both strings use slash-separated
formatting.

## Read semantics

Signature:

```python
await mailbox.get(filter=None, after_id=None, limit=100, order="oldest_first")
```

Important rules:

- default `limit=100`
- pass `limit=None` explicitly for an unbounded scan
- `after_id` is a UUIDv7 ordering cursor
- results are immutable snapshots

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

It intentionally does not expose `get`, `consume`, or `compact`.

## Producer supervision

`MailboxProducerSupervisor` runs producers in restartable supervision loops.

- producers are registered by name
- `start()` launches supervisor tasks
- `stop()` prevents further restarts and waits for shutdown
- producer return or exception is treated as a failure/restart condition
- restart uses bounded exponential backoff

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
- exact channel indexes (`mailbox:{namespace}:channel:{quote(channel)}`)
- unconsumed index
- consumed index
- consumed-info hash

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

## Concurrency model

Multiple `RedisMailbox` instances may point at the same Redis namespace.

This is normal for:

- separate producer and consumer processes
- separate UI or debug readers
- horizontally scaled workers performing polling reads

## Redis client lifecycle and errors

`RedisMailbox` does not own the Redis client lifecycle.

- caller creates the `redis.asyncio.Redis` client
- caller closes it
- mailbox methods surface Redis errors rather than silently swallowing them

## Suggested usage boundaries

Use this mailbox when you need:

- agent inbox semantics
- recent history lookups
- incremental polling via UUIDv7 cursor
- explicit consume state
- simple supervised producers

Do not treat it as:

- a consumer-group queue
- a distributed exactly-once system
- a replacement for Kafka/RabbitMQ-style broker semantics

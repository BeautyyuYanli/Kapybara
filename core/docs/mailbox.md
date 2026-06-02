# Mailbox Guide

## Overview

`kapy_mailbox` provides a PostgreSQL-oriented mailbox layer split into three
public surfaces:

- `PostgresMailboxWriter`: write-only, producer-facing, namespace-explicit
- `PostgresMailboxInbox`: read/consume only, bound to one namespace
- `PostgresMailboxMaintenance`: manual and automatic compaction for explicit
  namespaces

The package is intentionally **not** a broker, consumer-group queue, or
exactly-once delivery system. It is a mailbox/event-log abstraction with UUIDv7
ordering, exact-channel filtering, explicit consume state, and retention-based
cleanup.

The runtime components do not own database lifecycles. For production use,
`kapy_mailbox` ships `SQLModelPostgresMailboxStorage`, which uses SQLModel table
definitions on top of SQLAlchemy's async engine with the `psycopg` async
PostgreSQL driver. Advanced callers may still inject a custom adapter that
implements the `PostgresMailboxStorage` protocol.

## Design goals

1. **Writer, inbox, and maintenance are different objects**
   - producers do not derive a writer from a namespace-bound inbox
   - consumers do not receive write methods
   - destructive maintenance does not live on inbox objects

2. **Ordering uses UUIDv7**
   - every stored message gets a mailbox-assigned UUIDv7 id
   - that id is both the public message id and the mailbox ordering cursor

3. **Incremental reads use pure cursors**
   - `get(..., after_id=id)` only requires a syntactically valid UUIDv7 string
   - the referenced message does not need to still exist in storage

4. **Consume state is explicit**
   - `consume()` updates consume metadata
   - reads observe current consume state through `MessageFilter(consumed=...)`

5. **Automatic compaction is explicit**
   - callers pass the namespace list themselves
   - retention policy is provided per namespace
   - there is no automatic namespace discovery

## Public API surface

### Main types

- `PostgresMailboxWriter`
- `PostgresMailboxInbox`
- `PostgresMailboxMaintenance`
- `MailboxProducerSupervisor`

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
- `NamespaceNotAllowedError`
- `ProducerAlreadyRegisteredError`
- `ProducerSupervisorClosedError`

## Quick start

```python
from kapy_mailbox import (
    MessageFilter,
    PostgresMailboxInbox,
    PostgresMailboxMaintenance,
    PostgresMailboxWriter,
    RetentionPolicy,
    SQLModelPostgresMailboxStorage,
)

storage = SQLModelPostgresMailboxStorage.from_dsn(
    "postgresql://user:password@localhost:5432/kapybara"
)
await storage.create_schema()

writer = PostgresMailboxWriter(storage)
inbox = PostgresMailboxInbox(storage, namespace="agent-main")
maintenance = PostgresMailboxMaintenance(storage)

await writer.put(
    namespace="agent-main",
    channel="telegram/chat/1",
    payload={"text": "hello"},
    producer="telegram",
)

messages = await inbox.get(MessageFilter(consumed=False), order="oldest_first")
await inbox.consume([message.id for message in messages], consumer="agent-loop")

await maintenance.compact(
    namespace="agent-main",
    retention=RetentionPolicy(max_age=timedelta(days=3)),
)

await storage.dispose()
```

## Schema setup

`SQLModelPostgresMailboxStorage.create_schema()` creates the tables and indexes
used by the mailbox:

- `mailbox_messages`
- `mailbox_compaction_locks`

The timestamp columns used for message ordering and compaction leases are created
as timezone-aware PostgreSQL timestamps so the mailbox's UTC datetime semantics
round-trip correctly through the database.

Applications should run it during installation, migration, or startup before
serving mailbox traffic. The adapter does not auto-create schema during normal
writer/inbox/maintenance operations.

## Writer semantics

`PostgresMailboxWriter` is namespace-agnostic and write-only.

```python
await writer.put(
    namespace="agent-a",
    channel="telegram/chat/1",
    payload={"text": "hello"},
    producer="telegram",
)
```

```python
await writer.put_many(
    namespace="agent-b",
    items=[
        MessageInput(channel="telegram/chat/1", payload={"text": "a"}),
        MessageInput(channel="telegram/chat/2", payload={"text": "b"}),
    ],
    producer="telegram",
)
```

Important rules:

- `namespace` is required on every write
- one `put_many(...)` call targets exactly one namespace
- `allowed_namespaces=None` allows any namespace
- when an allow-list is configured, disallowed targets raise
  `NamespaceNotAllowedError` before any storage mutation
- the writer intentionally does not expose `get`, `consume`, or `compact`

## Inbox semantics

`PostgresMailboxInbox` is bound to one namespace.

```python
inbox = PostgresMailboxInbox(storage, namespace="agent-a")
messages = await inbox.get(MessageFilter(consumed=False), after_id=cursor, limit=50)
result = await inbox.consume([message.id for message in messages], consumer="agent")
```

Important rules:

- default `limit=100`
- pass `limit=None` explicitly for an unbounded scan
- `after_id` is a UUIDv7 ordering cursor
- `channel` filtering is exact, not prefix-based
- `strict=True` on `consume()` raises `UnknownMessageError` for missing ids
- inboxes do not expose `put`, `put_many`, `writer`, or `compact`

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

Mailbox channels reuse the existing slash-separated channel validation rules:

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

Filtering treats `channel` as one exact identifier, not a subtree root.

## Producer supervision

`MailboxProducerSupervisor` still supervises long-running producers, but it now
accepts a `PostgresMailboxWriter` directly.

```python
writer = PostgresMailboxWriter(storage)
supervisor = MailboxProducerSupervisor(writer)
```

Producer return or exception is treated as a failure/restart condition, and
restart uses bounded exponential backoff.

## Manual compaction

Compaction is owned by `PostgresMailboxMaintenance`.

```python
result = await maintenance.compact(
    namespace="agent-a",
    retention=RetentionPolicy(max_age=timedelta(days=3)),
)
```

Compaction only affects the explicit namespace passed to the call.

## Automatic compaction

Automatic compaction has two public entry points and no standalone
`MailboxAutoCompactor` object.

### Context-managed background timer

```python
retention_policies = {
    "agent-a": RetentionPolicy(max_age=timedelta(days=3)),
    "agent-b": RetentionPolicy(max_messages=1000),
}

async with maintenance.auto_compacting(
    namespaces=["agent-a", "agent-b"],
    retention_provider=lambda namespace: retention_policies.get(namespace),
    interval=timedelta(minutes=15),
):
    await run_application_until_shutdown()
```

Entering the context starts one background timer task. Exiting the context is
the only public stop signal.

### Blocking forever runner

```python
await maintenance.run_auto_compact_forever(
    namespaces=["agent-a", "agent-b"],
    retention_provider=lambda namespace: retention_policies.get(namespace),
    interval=timedelta(minutes=15),
)
```

This blocks in the current task until that task is cancelled.

### Automatic compaction rules

- only the explicit `namespaces=[...]` list is iterated
- the retention provider is queried for each namespace on each tick
- provider returning `None` skips that namespace
- a lock conflict or failure in one namespace does not stop others
- no cross-namespace compact operation exists; each namespace is compacted
  independently

## Suggested usage boundaries

Use this mailbox when you need:

- agent inbox semantics
- recent history lookups
- incremental polling via UUIDv7 cursor
- explicit consume state
- simple supervised producers
- retention-based cleanup over explicit namespace lists

Do not treat it as:

- a consumer-group queue
- a distributed exactly-once system
- a replacement for Kafka/RabbitMQ-style broker semantics
- a cross-namespace read or consume API

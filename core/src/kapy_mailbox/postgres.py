"""PostgreSQL-backed mailbox implementation split by responsibility.

This module exposes three first-class mailbox surfaces:

- `PostgresMailboxWriter` for namespace-explicit writes
- `PostgresMailboxInbox` for single-namespace reads and consume
- `PostgresMailboxMaintenance` for namespace-explicit manual and automatic
  compaction

The public classes do not own connection lifecycles. Instead they depend on a
`PostgresMailboxStorage` adapter protocol. This module also ships
`SQLModelPostgresMailboxStorage`, a production adapter that uses SQLModel models
on top of SQLAlchemy's async engine with psycopg's async PostgreSQL driver.
Tests still use a fake adapter for mailbox semantics, while the SQLModel adapter
provides real schema creation and database I/O for applications.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from logging import Logger, getLogger
from typing import ClassVar, Literal, NoReturn, Protocol, cast

import uuid6
from sqlalchemy import Column, DateTime, Index, Table, delete, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import Field, SQLModel

from k.agent.channels import validate_channel_path
from kapy_mailbox.exceptions import (
    InvalidChannelError,
    InvalidMessageWindowError,
    NamespaceNotAllowedError,
    ProducerAlreadyRegisteredError,
    ProducerSupervisorClosedError,
    UnknownMessageError,
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
    _ProducerRuntime,
)
from kapy_mailbox.serialization import JSONMessageSerializer, MessageSerializer

DEFAULT_COMPACT_INTERVAL = timedelta(minutes=15)
DEFAULT_COMPACT_LOCK_TTL = timedelta(minutes=5)
DEFAULT_COMPACT_LOCK_RENEW_INTERVAL = timedelta(seconds=60)
DEFAULT_QUERY_PAGE_SIZE = 100
DEFAULT_COMPACT_BATCH_SIZE = 100

type NamespaceRetentionProvider = Callable[[str], RetentionPolicy | None]
type _SourceKind = Literal["timeline", "unconsumed", "consumed", "channel"]
type MailboxProducer = Callable[
    [PostgresMailboxWriter, CancellationToken], Awaitable[None]
]


class Clock(Protocol):
    """Clock protocol used by mailbox components for deterministic tests."""

    def now(self) -> datetime:
        """Return the current time."""

        ...


class PostgresMailboxStorage(Protocol):
    """Driver adapter protocol for the PostgreSQL mailbox schema.

    Implementations are responsible for translating these high-level operations
    into SQL against PostgreSQL tables. The mailbox layer keeps business
    semantics here so callers are not tied to one specific async driver. The
    bundled `SQLModelPostgresMailboxStorage` class is the default production
    implementation.

    Custom adapters must preserve the mailbox's public contracts rather than
    merely storing equivalent data somewhere:

    - every method is namespace-local; no operation may leak ids or state across
      namespaces
    - message ordering is UUIDv7 lexicographic order, not database insertion
      order
    - read helpers must preserve the mailbox's pure-cursor semantics, where
      `after_id` and per-page `cursor` resume strictly by UUID comparison even if
      the referenced row no longer exists
    - missing message rows are represented as `None` in aligned loader results
      instead of being dropped or re-ordered
    - consume and compaction helpers must update all derived mailbox state for a
      namespace, not only one physical table
    """

    async def append_messages(
        self,
        namespace: str,
        rows: list[_StoredMessageRow],
    ) -> None:
        """Persist messages and all derived ordering/consume rows atomically.

        One call represents one `put_many(...)` write into a single namespace.
        Successful completion must make the message payloads readable through the
        timeline, exact-channel, and unconsumed mailbox views for that namespace.
        """

        ...

    async def scan_message_ids(
        self,
        namespace: str,
        *,
        source: _SourceKind,
        channel: str | None,
        after_id: str | None,
        cursor: str | None,
        order: MessageOrder,
        limit: int | None,
    ) -> list[str]:
        """Return ordered message ids from one mailbox read source.

        `source="timeline"` reads the namespace-global mailbox order,
        `"channel"` reads one exact channel, `"unconsumed"` reads only currently
        unconsumed ids, and `"consumed"` reads only currently consumed ids.
        Implementations must honor `after_id` and `cursor` as strict UUIDv7
        boundaries and return ids in the requested oldest/newest order without
        consulting any cross-namespace state.
        """

        ...

    async def load_messages(
        self,
        namespace: str,
        message_ids: list[str],
    ) -> list[str | None]:
        """Return serialized message rows aligned with the requested ids.

        The returned list must have the same length and positional order as
        `message_ids`. Missing rows are represented as `None` so mailbox readers
        can keep cursor semantics even when a message disappears between id scan
        and payload load.
        """

        ...

    async def load_consumed_infos(
        self,
        namespace: str,
        message_ids: list[str],
    ) -> list[str | None]:
        """Return serialized consumed-info rows aligned with the requested ids.

        Like `load_messages(...)`, the result must preserve input order and use
        `None` for ids that have no consumed-info row or no backing message row.
        """

        ...

    async def consume_messages(
        self,
        namespace: str,
        *,
        message_ids: list[str],
        consumed_info_json: str,
        strict: bool,
    ) -> tuple[list[str], list[str], list[str]]:
        """Atomically mark messages consumed and classify all requested ids.

        The returned tuple is `(consumed, already_consumed, not_found)`.
        `strict=False` must classify every requested id without raising, while
        `strict=True` must preserve the mailbox's all-or-nothing existence check:
        if any id is unknown, no consume-state mutation is applied and those ids
        are returned in `not_found`.
        """

        ...

    async def is_unconsumed(self, namespace: str, message_id: str) -> bool:
        """Return whether a message is still in the unconsumed read model.

        This predicate is used by `keep_unconsumed=True` compaction. Returning an
        incorrect value can cause manual or automatic compaction to delete rows
        that should be preserved.
        """

        ...

    async def delete_messages(
        self,
        namespace: str,
        messages: list[Message],
    ) -> int:
        """Delete messages and return how many consumed-info rows were removed.

        Deletion must remove the namespace-local mailbox state implied by those
        messages: the stored payload rows themselves plus their presence in the
        timeline, exact-channel, consumed, and unconsumed read models. The
        integer result counts how many consumed-info rows existed for the deleted
        messages.
        """

        ...

    async def try_acquire_compact_lock(
        self,
        namespace: str,
        token: str,
        *,
        ttl: timedelta,
        now: datetime,
    ) -> bool:
        """Try to acquire the namespace compaction lock.

        Normal lock conflicts are not exceptional: if another worker still owns
        an active lease for `namespace`, this method returns `False` rather than
        surfacing uniqueness or race errors.
        """

        ...

    async def renew_compact_lock(
        self,
        namespace: str,
        token: str,
        *,
        ttl: timedelta,
        now: datetime,
    ) -> bool:
        """Renew the namespace compaction lock if still owned by `token`."""

        ...

    async def release_compact_lock(self, namespace: str, token: str) -> None:
        """Release the namespace compaction lock if still owned by `token`."""

        ...


class _MailboxMessageRecord(SQLModel, table=True):
    """Normalized mailbox row stored in PostgreSQL.

    One row carries the immutable serialized message plus nullable consumed-info
    overlay. Channel, created-at, and consume-state columns are duplicated from
    the JSON payload so PostgreSQL can satisfy mailbox read models with ordinary
    indexes instead of maintaining a separate registry or broker-specific data
    structures.
    """

    __tablename__: ClassVar[str] = "mailbox_messages"  # pyright: ignore[reportIncompatibleVariableOverride]
    __table_args__ = (
        Index(
            "ix_mailbox_messages_namespace_message_id",
            "namespace",
            "message_id",
        ),
        Index(
            "ix_mailbox_messages_namespace_channel_message_id",
            "namespace",
            "channel",
            "message_id",
        ),
        Index(
            "ix_mailbox_messages_namespace_unconsumed_message_id",
            "namespace",
            "message_id",
            postgresql_where=text("raw_consumed_info IS NULL"),
        ),
        Index(
            "ix_mailbox_messages_namespace_consumed_message_id",
            "namespace",
            "message_id",
            postgresql_where=text("raw_consumed_info IS NOT NULL"),
        ),
    )

    namespace: str = Field(primary_key=True, max_length=255)
    message_id: str = Field(primary_key=True, max_length=36)
    channel: str
    created_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    producer: str | None = None
    raw_message: str
    raw_consumed_info: str | None = None


class _MailboxCompactionLockRecord(SQLModel, table=True):
    """Namespace-scoped compaction lease row.

    Automatic compaction never scans for namespaces. It only uses this table to
    coordinate mutually exclusive compaction for namespaces the caller explicitly
    provided to maintenance.
    """

    __tablename__: ClassVar[str] = "mailbox_compaction_locks"  # pyright: ignore[reportIncompatibleVariableOverride]

    namespace: str = Field(primary_key=True, max_length=255)
    token: str
    expires_at: datetime = Field(
        sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class SQLModelPostgresMailboxStorage:
    """Production mailbox storage adapter backed by SQLModel and async psycopg.

    Applications can either inject an existing async engine or build one from a
    PostgreSQL DSN with `from_dsn(...)`. Schema setup is explicit: call
    `create_schema()` during installation or startup to create the mailbox
    tables/indexes managed by this adapter.

    Engine lifecycle ownership follows construction style:

    - when callers pass an already-configured `AsyncEngine` to `__init__`, they
      remain the lifecycle owner and should only call `dispose()` if they intend
      to shut that shared engine down
    - when callers use `from_dsn(...)`, this adapter created the engine and
      `dispose()` is the matching cleanup hook

    In other words, `dispose()` always disposes the wrapped engine; callers must
    decide whether that engine is adapter-owned or shared.
    """

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        _sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._engine = engine
        self._sessionmaker = _sessionmaker or async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    @classmethod
    def from_dsn(
        cls,
        dsn: str,
        *,
        echo: bool = False,
        connect_args: Mapping[str, object] | None = None,
        engine_kwargs: Mapping[str, object] | None = None,
    ) -> SQLModelPostgresMailboxStorage:
        """Build a storage adapter from a PostgreSQL DSN.

        The DSN may use either `postgresql://...` or `postgresql+psycopg://...`.
        It is normalized to SQLAlchemy's async psycopg dialect before the engine
        is created.
        """

        normalized_dsn = _normalize_postgres_dsn(dsn)
        kwargs: dict[str, object] = {"echo": echo}
        if connect_args is not None:
            kwargs["connect_args"] = dict(connect_args)
        if engine_kwargs is not None:
            kwargs.update(dict(engine_kwargs))
        engine = create_async_engine(normalized_dsn, **kwargs)
        return cls(engine)

    async def create_schema(self) -> None:
        """Create the mailbox tables and indexes managed by this adapter."""

        async with self._engine.begin() as connection:
            await connection.run_sync(SQLModel.metadata.create_all)

    async def dispose(self) -> None:
        """Dispose the wrapped async engine and its connection pool.

        This is the normal cleanup path for adapters created with `from_dsn(...)`.
        If the adapter wraps a shared externally managed engine, callers should
        only invoke `dispose()` when they intentionally want to shut that shared
        engine down too.
        """

        await self._engine.dispose()

    async def append_messages(
        self,
        namespace: str,
        rows: list[_StoredMessageRow],
    ) -> None:
        async with self._sessionmaker.begin() as session:
            session.add_all(
                [
                    _MailboxMessageRecord(
                        namespace=namespace,
                        message_id=row.message.id,
                        channel=row.message.channel,
                        created_at=row.message.created_at,
                        producer=row.message.producer,
                        raw_message=row.raw_message,
                    )
                    for row in rows
                ]
            )

    async def scan_message_ids(
        self,
        namespace: str,
        *,
        source: _SourceKind,
        channel: str | None,
        after_id: str | None,
        cursor: str | None,
        order: MessageOrder,
        limit: int | None,
    ) -> list[str]:
        stmt = select(_MESSAGE_TABLE.c.message_id).where(
            _MESSAGE_TABLE.c.namespace == namespace
        )
        if source == "channel":
            stmt = stmt.where(_MESSAGE_TABLE.c.channel == (channel or ""))
        elif source == "unconsumed":
            stmt = stmt.where(_MESSAGE_TABLE.c.raw_consumed_info.is_(None))
        elif source == "consumed":
            stmt = stmt.where(_MESSAGE_TABLE.c.raw_consumed_info.is_not(None))

        if order == "oldest_first":
            start = cursor or after_id
            if start is not None:
                stmt = stmt.where(_MESSAGE_TABLE.c.message_id > start)
            stmt = stmt.order_by(_MESSAGE_TABLE.c.message_id.asc())
        else:
            if after_id is not None:
                stmt = stmt.where(_MESSAGE_TABLE.c.message_id > after_id)
            if cursor is not None:
                stmt = stmt.where(_MESSAGE_TABLE.c.message_id < cursor)
            stmt = stmt.order_by(_MESSAGE_TABLE.c.message_id.desc())

        if limit is not None:
            stmt = stmt.limit(limit)

        async with self._sessionmaker() as session:
            result = await session.execute(stmt)
            return list(result.scalars())

    async def load_messages(
        self,
        namespace: str,
        message_ids: list[str],
    ) -> list[str | None]:
        if not message_ids:
            return []

        stmt = (
            select(_MESSAGE_TABLE.c.message_id, _MESSAGE_TABLE.c.raw_message)
            .where(_MESSAGE_TABLE.c.namespace == namespace)
            .where(_MESSAGE_TABLE.c.message_id.in_(message_ids))
        )
        async with self._sessionmaker() as session:
            result = await session.execute(stmt)
            by_id = {message_id: raw for message_id, raw in result.all()}
        return [by_id.get(message_id) for message_id in message_ids]

    async def load_consumed_infos(
        self,
        namespace: str,
        message_ids: list[str],
    ) -> list[str | None]:
        if not message_ids:
            return []

        stmt = (
            select(
                _MESSAGE_TABLE.c.message_id,
                _MESSAGE_TABLE.c.raw_consumed_info,
            )
            .where(_MESSAGE_TABLE.c.namespace == namespace)
            .where(_MESSAGE_TABLE.c.message_id.in_(message_ids))
        )
        async with self._sessionmaker() as session:
            result = await session.execute(stmt)
            by_id = {message_id: raw for message_id, raw in result.all()}
        return [by_id.get(message_id) for message_id in message_ids]

    async def consume_messages(
        self,
        namespace: str,
        *,
        message_ids: list[str],
        consumed_info_json: str,
        strict: bool,
    ) -> tuple[list[str], list[str], list[str]]:
        async with self._sessionmaker.begin() as session:
            stmt = (
                select(_MailboxMessageRecord)
                .where(_MESSAGE_TABLE.c.namespace == namespace)
                .where(_MESSAGE_TABLE.c.message_id.in_(message_ids))
                .with_for_update()
            )
            result = await session.execute(stmt)
            rows = {row.message_id: row for row in result.scalars()}

            missing = [
                message_id for message_id in message_ids if message_id not in rows
            ]
            if strict and missing:
                return [], [], missing

            consumed: list[str] = []
            already_consumed: list[str] = []
            not_found: list[str] = []
            for message_id in message_ids:
                row = rows.get(message_id)
                if row is None:
                    not_found.append(message_id)
                    continue
                if row.raw_consumed_info is not None:
                    already_consumed.append(message_id)
                    continue
                row.raw_consumed_info = consumed_info_json
                consumed.append(message_id)

            return consumed, already_consumed, not_found

    async def is_unconsumed(self, namespace: str, message_id: str) -> bool:
        stmt = (
            select(_MESSAGE_TABLE.c.message_id)
            .where(_MESSAGE_TABLE.c.namespace == namespace)
            .where(_MESSAGE_TABLE.c.message_id == message_id)
            .where(_MESSAGE_TABLE.c.raw_consumed_info.is_(None))
            .limit(1)
        )
        async with self._sessionmaker() as session:
            result = await session.execute(stmt)
            return result.scalar_one_or_none() is not None

    async def delete_messages(
        self,
        namespace: str,
        messages: list[Message],
    ) -> int:
        if not messages:
            return 0

        message_ids = [message.id for message in messages]
        async with self._sessionmaker.begin() as session:
            existing_result = await session.execute(
                select(_MESSAGE_TABLE.c.raw_consumed_info)
                .where(_MESSAGE_TABLE.c.namespace == namespace)
                .where(_MESSAGE_TABLE.c.message_id.in_(message_ids))
                .with_for_update()
            )
            consumed_info_removed = sum(
                1
                for raw_consumed_info in existing_result.scalars()
                if raw_consumed_info is not None
            )
            await session.execute(
                delete(_MailboxMessageRecord)
                .where(_MESSAGE_TABLE.c.namespace == namespace)
                .where(_MESSAGE_TABLE.c.message_id.in_(message_ids))
            )
            return consumed_info_removed

    async def try_acquire_compact_lock(
        self,
        namespace: str,
        token: str,
        *,
        ttl: timedelta,
        now: datetime,
    ) -> bool:
        expires_at = now + ttl
        async with self._sessionmaker.begin() as session:
            # Use one PostgreSQL upsert so the first-acquire path is safe under
            # concurrent maintenance workers: an active lock yields no returned
            # row, while an absent or expired lock is inserted/replaced atomically.
            result = await session.execute(
                _acquire_compact_lock_statement(
                    namespace=namespace,
                    token=token,
                    expires_at=expires_at,
                    now=now,
                )
            )
            return result.scalar_one_or_none() is not None

    async def renew_compact_lock(
        self,
        namespace: str,
        token: str,
        *,
        ttl: timedelta,
        now: datetime,
    ) -> bool:
        async with self._sessionmaker.begin() as session:
            row = await self._load_lock_for_update(session, namespace)
            if row is None or row.token != token:
                return False
            row.expires_at = now + ttl
            return True

    async def release_compact_lock(self, namespace: str, token: str) -> None:
        async with self._sessionmaker.begin() as session:
            row = await self._load_lock_for_update(session, namespace)
            if row is None or row.token != token:
                return
            await session.delete(row)

    async def _load_lock_for_update(
        self,
        session: AsyncSession,
        namespace: str,
    ) -> _MailboxCompactionLockRecord | None:
        result = await session.execute(
            select(_MailboxCompactionLockRecord)
            .where(_LOCK_TABLE.c.namespace == namespace)
            .with_for_update()
        )
        return result.scalar_one_or_none()


def _sqlmodel_table(model: type[SQLModel]) -> Table:
    """Return the SQLAlchemy table SQLModel attaches to a table model.

    SQLModel creates `__table__` dynamically, which pyright cannot see on the
    Python class. Reading the class namespace keeps that dynamic boundary in one
    typed helper instead of scattering ignores around query construction.
    """

    return cast(Table, vars(model)["__table__"])


_MESSAGE_TABLE = _sqlmodel_table(_MailboxMessageRecord)
_LOCK_TABLE = _sqlmodel_table(_MailboxCompactionLockRecord)


def _acquire_compact_lock_statement(
    *,
    namespace: str,
    token: str,
    expires_at: datetime,
    now: datetime,
):
    """Return the PostgreSQL upsert used for namespace compaction locks.

    The statement inserts a new lease when no row exists and only overwrites an
    existing row when the stored lease has expired. Active-lock conflicts are
    therefore reported as "no row returned" instead of surfacing uniqueness
    errors during normal multi-worker races.
    """

    insert_stmt = pg_insert(_MailboxCompactionLockRecord).values(
        namespace=namespace,
        token=token,
        expires_at=expires_at,
    )
    return insert_stmt.on_conflict_do_update(
        index_elements=[_LOCK_TABLE.c.namespace],
        set_={
            "token": token,
            "expires_at": expires_at,
        },
        where=_LOCK_TABLE.c.expires_at <= now,
    ).returning(_LOCK_TABLE.c.namespace)


class _SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class _UUIDv7Factory:
    """Generate UUIDv7 ids from the mailbox clock.

    `uuid6.uuid7()` is used directly for the normal system-clock path. When
    tests inject a custom clock, the factory builds a UUIDv7 through `uuid6.UUID`
    so the id timestamp and derived `created_at` stay aligned with that clock
    while still preserving monotonic ordering within the process.
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
class _StoredMessageRow:
    """One serialized message plus the fields needed by derived mailbox tables."""

    message: Message
    raw_message: str


@dataclass(slots=True)
class _CandidateSource:
    """Mutable cursor for one logical PostgreSQL read source.

    Read pagination still follows the mailbox's pure-cursor contract: one source
    is chosen up front, and later filters are applied after loading candidate
    messages so `after_id` semantics stay stable even if a message disappears.
    """

    source: _SourceKind
    channel: str | None = None
    after_id: str | None = None
    cursor: str | None = None


class PostgresMailboxWriter:
    """Write-only producer surface for the PostgreSQL mailbox.

    Writers are namespace-agnostic. Every write must explicitly choose the target
    namespace, which keeps producer permissions and routing decisions visible at
    each call site. When an allow-list is configured, targeting any other
    namespace raises `NamespaceNotAllowedError` before any storage mutation.
    """

    def __init__(
        self,
        storage: PostgresMailboxStorage,
        *,
        serializer: MessageSerializer | None = None,
        clock: Clock | None = None,
        allowed_namespaces: Iterable[str] | None = None,
    ) -> None:
        self._storage = storage
        self._serializer = serializer or JSONMessageSerializer()
        self._clock = clock or _SystemClock()
        self._uuid_factory = _UUIDv7Factory(clock=self._clock)
        self._allowed_namespaces = (
            None if allowed_namespaces is None else frozenset(allowed_namespaces)
        )

    def _validate_namespace(self, namespace: str) -> None:
        if (
            self._allowed_namespaces is not None
            and namespace not in self._allowed_namespaces
        ):
            raise NamespaceNotAllowedError(
                f"Writer cannot write to namespace {namespace!r}"
            )

    def _normalize_channel(self, channel: str, *, field_name: str) -> str:
        try:
            return validate_channel_path(channel, field_name=field_name)
        except (TypeError, ValueError) as exc:
            raise InvalidChannelError(str(exc)) from exc

    async def put(
        self,
        *,
        namespace: str,
        channel: str,
        payload: JsonValue,
        producer: str | None = None,
    ) -> Message:
        """Append one message to `namespace` and return its immutable snapshot.

        `namespace` is required and keyword-only: writers have no implicit default
        namespace. If this writer was created with an allow-list and `namespace`
        falls outside it, `NamespaceNotAllowedError` is raised before any storage
        mutation is attempted.
        """

        messages = await self.put_many(
            namespace=namespace,
            items=[MessageInput(channel=channel, payload=payload, producer=producer)],
        )
        return messages[0]

    async def put_many(
        self,
        *,
        namespace: str,
        items: Iterable[MessageInput],
        producer: str | None = None,
    ) -> list[Message]:
        """Append several messages to one namespace with one storage transaction.

        Every item in one call targets the same explicit `namespace`. The call
        preserves input order, uses the call-level `producer` as a fallback when
        `MessageInput.producer` is unset, and raises
        `NamespaceNotAllowedError` before any write when an allow-list rejects the
        requested namespace.
        """

        self._validate_namespace(namespace)
        normalized_items = list(items)
        if not normalized_items:
            return []

        rows: list[_StoredMessageRow] = []
        for index, item in enumerate(normalized_items):
            message_id, created_at = self._uuid_factory.new()
            message = Message(
                id=message_id,
                channel=self._normalize_channel(
                    item.channel,
                    field_name=f"items[{index}].channel",
                ),
                payload=self._serializer.normalize_payload(item.payload),
                created_at=created_at,
                producer=item.producer if item.producer is not None else producer,
            )
            rows.append(
                _StoredMessageRow(
                    message=message,
                    raw_message=self._serializer.dump_message(message),
                )
            )

        await self._storage.append_messages(namespace, rows)
        return [row.message for row in rows]


class PostgresMailboxInbox:
    """Single-namespace read and consume API for one mailbox inbox.

    Inboxes intentionally do not expose writes or maintenance. They bind one
    namespace and provide pure-cursor reads plus explicit consume-state updates
    within that namespace only.
    """

    def __init__(
        self,
        storage: PostgresMailboxStorage,
        *,
        namespace: str,
        serializer: MessageSerializer | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._storage = storage
        self._namespace = namespace
        self._serializer = serializer or JSONMessageSerializer()
        self._clock = clock or _SystemClock()

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
        strictly after a UUIDv7 cursor without requiring that cursor to still
        exist in storage, and `order` chooses oldest-first or newest-first
        lexicographic UUID order.
        """

        normalized_after_id = self._normalize_after_id(after_id)
        if limit is not None and limit < 0:
            raise InvalidMessageWindowError("limit must be >= 0 or None")
        if order not in {"oldest_first", "newest_first"}:
            raise InvalidMessageWindowError(f"Unsupported order: {order!r}")
        if limit == 0:
            return []

        message_filter = filter or MessageFilter()
        candidate_source = self._candidate_source(
            message_filter,
            after_id=normalized_after_id,
        )

        matched: list[Message] = []
        while True:
            remaining = None if limit is None else limit - len(matched)
            if remaining == 0:
                return matched

            candidate_ids = await self._scan_ids_page(
                candidate_source,
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
        mutation so callers can treat consume as all-or-nothing with respect to
        message existence while keeping the operation idempotent for already
        consumed messages.
        """

        ids = list(message_ids)
        if not ids:
            return ConsumeResult(consumed=[], already_consumed=[], not_found=[])

        unique_ids = list(dict.fromkeys(ids))
        consumed_at = _normalize_datetime(self._clock.now())
        consumed, already_consumed, not_found = await self._storage.consume_messages(
            self._namespace,
            message_ids=unique_ids,
            consumed_info_json=self._serializer.dump_consumed_info(
                consumed_at,
                consumed_by=consumer,
            ),
            strict=strict,
        )
        if strict and not_found:
            raise UnknownMessageError(not_found)
        return ConsumeResult(
            consumed=consumed,
            already_consumed=already_consumed,
            not_found=not_found,
        )

    def _candidate_source(
        self,
        filter: MessageFilter,
        *,
        after_id: str | None,
    ) -> _CandidateSource:
        """Choose the single mailbox read source scanned for one inbox query."""

        if filter.channel is not None:
            return _CandidateSource(
                source="channel",
                channel=filter.channel,
                after_id=after_id,
            )
        if filter.consumed is True:
            return _CandidateSource(source="consumed", after_id=after_id)
        if filter.consumed is False:
            return _CandidateSource(source="unconsumed", after_id=after_id)
        return _CandidateSource(source="timeline", after_id=after_id)

    async def _load_messages(self, message_ids: list[str]) -> list[Message]:
        if not message_ids:
            return []

        raw_messages = await self._storage.load_messages(self._namespace, message_ids)
        raw_consumed_infos = await self._storage.load_consumed_infos(
            self._namespace,
            message_ids,
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
                    raw_message,
                    raw_consumed_info=raw_consumed_info,
                )
            )
        return messages

    async def _scan_ids_page(
        self,
        source: _CandidateSource,
        *,
        order: MessageOrder,
        limit: int,
    ) -> list[str]:
        message_ids = await self._storage.scan_message_ids(
            self._namespace,
            source=source.source,
            channel=source.channel,
            after_id=source.after_id,
            cursor=source.cursor,
            order=order,
            limit=limit,
        )
        if not message_ids:
            return []
        source.cursor = message_ids[-1]
        return message_ids

    def _normalize_after_id(self, after_id: str | None) -> str | None:
        if after_id is None:
            return None
        return _normalize_uuid7(after_id)


class PostgresMailboxMaintenance:
    """Namespace-explicit destructive maintenance for the PostgreSQL mailbox.

    Manual compaction requires a namespace and retention policy on every call.
    Automatic compaction is driven only by explicit namespace lists supplied by
    the caller; it never discovers namespaces by scanning storage.
    """

    def __init__(
        self,
        storage: PostgresMailboxStorage,
        *,
        serializer: MessageSerializer | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._storage = storage
        self._serializer = serializer or JSONMessageSerializer()
        self._clock = clock or _SystemClock()

    async def compact(
        self,
        *,
        namespace: str,
        retention: RetentionPolicy,
        _should_continue: Callable[[], bool] | None = None,
        _delete_batch_size: int = DEFAULT_COMPACT_BATCH_SIZE,
    ) -> CompactionResult:
        """Apply `retention` to one namespace and delete all derived mailbox rows.

        `_should_continue` and `_delete_batch_size` are internal hooks used by
        automatic compaction lock handling. Manual callers should only pass
        `namespace` and `retention`.
        """

        timeline_ids = await self._storage.scan_message_ids(
            namespace,
            source="timeline",
            channel=None,
            after_id=None,
            cursor=None,
            order="oldest_first",
            limit=None,
        )
        if not timeline_ids:
            return CompactionResult(
                messages_deleted=0,
                index_entries_removed=0,
                consumed_info_removed=0,
                skipped_unconsumed=0,
            )

        inbox = PostgresMailboxInbox(
            self._storage,
            namespace=namespace,
            serializer=self._serializer,
            clock=self._clock,
        )
        timeline_messages = await inbox._load_messages(timeline_ids)
        if not timeline_messages:
            return CompactionResult(
                messages_deleted=0,
                index_entries_removed=0,
                consumed_info_removed=0,
                skipped_unconsumed=0,
            )

        cutoff: datetime | None = None
        if retention.max_age is not None:
            cutoff = _normalize_datetime(self._clock.now()) - retention.max_age

        remaining_count_excess = 0
        if retention.max_messages is not None:
            remaining_count_excess = max(
                0,
                len(timeline_messages) - retention.max_messages,
            )

        messages_to_delete: list[Message] = []
        skipped_unconsumed = 0
        for message in timeline_messages:
            due_to_age = cutoff is not None and message.created_at <= cutoff
            due_to_count = remaining_count_excess > 0
            if not due_to_age and not due_to_count:
                continue
            if retention.keep_unconsumed and await self._storage.is_unconsumed(
                namespace,
                message.id,
            ):
                skipped_unconsumed += 1
                continue
            messages_to_delete.append(message)
            if due_to_count:
                remaining_count_excess -= 1

        if not messages_to_delete:
            return CompactionResult(
                messages_deleted=0,
                index_entries_removed=0,
                consumed_info_removed=0,
                skipped_unconsumed=skipped_unconsumed,
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
            consumed_info_removed += await self._storage.delete_messages(
                namespace,
                batch_messages,
            )
            deleted_messages += len(batch_messages)
            index_entries_removed += len(batch_messages) * 4

            if _should_continue is not None and not _should_continue():
                break

        return CompactionResult(
            messages_deleted=deleted_messages,
            index_entries_removed=index_entries_removed,
            consumed_info_removed=consumed_info_removed,
            skipped_unconsumed=skipped_unconsumed,
        )

    def auto_compacting(
        self,
        *,
        namespaces: Iterable[str],
        retention_provider: NamespaceRetentionProvider,
        interval: timedelta = DEFAULT_COMPACT_INTERVAL,
        use_postgres_lock: bool = True,
        lock_ttl: timedelta = DEFAULT_COMPACT_LOCK_TTL,
        lock_renew_interval: timedelta = DEFAULT_COMPACT_LOCK_RENEW_INTERVAL,
        jitter_ratio: float = 0.1,
        logger: Logger | None = None,
    ) -> AbstractAsyncContextManager[None]:
        """Return a context manager that runs automatic compaction in background.

        Entering the context starts one timer task. Leaving the context is the
        only public stop signal; it cancels the task and waits for it to finish.
        The loop iterates only the explicit `namespaces` list provided here.
        """

        return _AutoCompactionSession(
            maintenance=self,
            namespaces=tuple(namespaces),
            retention_provider=retention_provider,
            interval=interval,
            use_postgres_lock=use_postgres_lock,
            lock_ttl=lock_ttl,
            lock_renew_interval=lock_renew_interval,
            jitter_ratio=jitter_ratio,
            logger=logger or getLogger(__name__),
        )

    async def run_auto_compact_forever(
        self,
        *,
        namespaces: Iterable[str],
        retention_provider: NamespaceRetentionProvider,
        interval: timedelta = DEFAULT_COMPACT_INTERVAL,
        use_postgres_lock: bool = True,
        lock_ttl: timedelta = DEFAULT_COMPACT_LOCK_TTL,
        lock_renew_interval: timedelta = DEFAULT_COMPACT_LOCK_RENEW_INTERVAL,
        jitter_ratio: float = 0.1,
        logger: Logger | None = None,
    ) -> NoReturn:
        """Block the current task and run automatic compaction until cancelled.

        Unlike `auto_compacting(...)`, this method does not create hidden public
        lifecycle state. The caller owns the task and stops it by cancelling that
        task or shutting down the process.
        """

        await self._run_auto_compact_loop(
            namespaces=tuple(namespaces),
            retention_provider=retention_provider,
            interval=interval,
            use_postgres_lock=use_postgres_lock,
            lock_ttl=lock_ttl,
            lock_renew_interval=lock_renew_interval,
            jitter_ratio=jitter_ratio,
            logger=logger or getLogger(__name__),
            stop_event=None,
        )
        raise AssertionError("automatic compaction loop terminated unexpectedly")

    async def _run_auto_compact_loop(
        self,
        *,
        namespaces: tuple[str, ...],
        retention_provider: NamespaceRetentionProvider,
        interval: timedelta,
        use_postgres_lock: bool,
        lock_ttl: timedelta,
        lock_renew_interval: timedelta,
        jitter_ratio: float,
        logger: Logger,
        stop_event: asyncio.Event | None,
    ) -> None:
        """Shared timer loop used by both auto-compaction entry points."""

        while True:
            if await self._wait_for_next_tick(
                interval=interval,
                jitter_ratio=jitter_ratio,
                stop_event=stop_event,
            ):
                return
            await self._run_auto_compact_tick(
                namespaces=namespaces,
                retention_provider=retention_provider,
                use_postgres_lock=use_postgres_lock,
                lock_ttl=lock_ttl,
                lock_renew_interval=lock_renew_interval,
                logger=logger,
                stop_event=stop_event,
            )

    async def _wait_for_next_tick(
        self,
        *,
        interval: timedelta,
        jitter_ratio: float,
        stop_event: asyncio.Event | None,
    ) -> bool:
        sleep_seconds = _sleep_seconds_with_jitter(interval, jitter_ratio)
        if stop_event is None:
            await asyncio.sleep(sleep_seconds)
            return False

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=sleep_seconds)
            return True
        except TimeoutError:
            return False

    async def _run_auto_compact_tick(
        self,
        *,
        namespaces: tuple[str, ...],
        retention_provider: NamespaceRetentionProvider,
        use_postgres_lock: bool,
        lock_ttl: timedelta,
        lock_renew_interval: timedelta,
        logger: Logger,
        stop_event: asyncio.Event | None,
    ) -> None:
        for namespace in namespaces:
            retention = retention_provider(namespace)
            if retention is None:
                logger.warning(
                    "Skipping mailbox compaction for namespace %s: no retention policy",
                    namespace,
                )
                continue

            try:
                if use_postgres_lock:
                    lock_token = uuid.uuid4().hex
                    if not await self._storage.try_acquire_compact_lock(
                        namespace,
                        lock_token,
                        ttl=lock_ttl,
                        now=_normalize_datetime(self._clock.now()),
                    ):
                        continue

                    lock_lost = asyncio.Event()
                    renew_stop = asyncio.Event()
                    renew_task = asyncio.create_task(
                        self._renew_compact_lock(
                            namespace,
                            lock_token,
                            renew_stop=renew_stop,
                            lock_lost=lock_lost,
                            lock_ttl=lock_ttl,
                            lock_renew_interval=lock_renew_interval,
                            logger=logger,
                            outer_stop_event=stop_event,
                        )
                    )
                    try:
                        await self.compact(
                            namespace=namespace,
                            retention=retention,
                            _should_continue=lambda lock_lost=lock_lost: (
                                not lock_lost.is_set()
                            ),
                        )
                    finally:
                        renew_stop.set()
                        renew_task.cancel()
                        await asyncio.gather(renew_task, return_exceptions=True)
                        await self._storage.release_compact_lock(namespace, lock_token)
                else:
                    await self.compact(namespace=namespace, retention=retention)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Automatic mailbox compaction failed for namespace %s",
                    namespace,
                )

    async def _renew_compact_lock(
        self,
        namespace: str,
        token: str,
        *,
        renew_stop: asyncio.Event,
        lock_lost: asyncio.Event,
        lock_ttl: timedelta,
        lock_renew_interval: timedelta,
        logger: Logger,
        outer_stop_event: asyncio.Event | None,
    ) -> None:
        while not renew_stop.is_set():
            try:
                await asyncio.wait_for(
                    renew_stop.wait(),
                    timeout=lock_renew_interval.total_seconds(),
                )
                return
            except TimeoutError:
                pass

            if outer_stop_event is not None and outer_stop_event.is_set():
                return

            try:
                renewed = await self._storage.renew_compact_lock(
                    namespace,
                    token,
                    ttl=lock_ttl,
                    now=_normalize_datetime(self._clock.now()),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                lock_lost.set()
                logger.exception(
                    "Mailbox compaction lock renewal failed for namespace %s",
                    namespace,
                )
                return
            if not renewed:
                lock_lost.set()
                return


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
        writer: PostgresMailboxWriter,
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


class _AutoCompactionSession(AbstractAsyncContextManager[None]):
    """Private async context manager that owns one background compaction task."""

    def __init__(
        self,
        *,
        maintenance: PostgresMailboxMaintenance,
        namespaces: tuple[str, ...],
        retention_provider: NamespaceRetentionProvider,
        interval: timedelta,
        use_postgres_lock: bool,
        lock_ttl: timedelta,
        lock_renew_interval: timedelta,
        jitter_ratio: float,
        logger: Logger,
    ) -> None:
        self._maintenance = maintenance
        self._namespaces = namespaces
        self._retention_provider = retention_provider
        self._interval = interval
        self._use_postgres_lock = use_postgres_lock
        self._lock_ttl = lock_ttl
        self._lock_renew_interval = lock_renew_interval
        self._jitter_ratio = jitter_ratio
        self._logger = logger
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> None:
        self._task = asyncio.create_task(
            self._maintenance._run_auto_compact_loop(
                namespaces=self._namespaces,
                retention_provider=self._retention_provider,
                interval=self._interval,
                use_postgres_lock=self._use_postgres_lock,
                lock_ttl=self._lock_ttl,
                lock_renew_interval=self._lock_renew_interval,
                jitter_ratio=self._jitter_ratio,
                logger=self._logger,
                stop_event=self._stop_event,
            )
        )
        return None

    async def __aexit__(self, *_: object) -> None:
        self._stop_event.set()
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


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


def _query_page_size(remaining: int | None) -> int:
    if remaining is None:
        return DEFAULT_QUERY_PAGE_SIZE
    return max(1, min(DEFAULT_QUERY_PAGE_SIZE, remaining))


def _sleep_seconds_with_jitter(interval: timedelta, jitter_ratio: float) -> float:
    base = interval.total_seconds()
    jitter = base * jitter_ratio
    if jitter == 0:
        return base
    return max(0.0, base + random.uniform(-jitter, jitter))


def _normalize_postgres_dsn(dsn: str) -> str:
    """Normalize a PostgreSQL DSN to SQLAlchemy's async psycopg dialect."""

    if dsn.startswith("postgresql+psycopg://"):
        return dsn
    if dsn.startswith("postgresql://"):
        return dsn.replace("postgresql://", "postgresql+psycopg://", 1)
    if dsn.startswith("postgres://"):
        return dsn.replace("postgres://", "postgresql+psycopg://", 1)
    return dsn

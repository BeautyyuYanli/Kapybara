from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import uuid6
from sqlalchemy.dialects import postgresql
from sqlmodel import SQLModel

import kapy_mailbox
import kapy_mailbox.postgres as mailbox_postgres_module
from kapy_mailbox import (
    InvalidMessageWindowError,
    JSONMessageSerializer,
    MailboxProducerSupervisor,
    Message,
    MessageFilter,
    MessageInput,
    NamespaceNotAllowedError,
    PostgresMailboxInbox,
    PostgresMailboxMaintenance,
    PostgresMailboxWriter,
    RestartPolicy,
    RetentionPolicy,
    SQLModelPostgresMailboxStorage,
)
from kapy_mailbox.exceptions import PayloadSerializationError, UnknownMessageError


@dataclass(slots=True)
class _ManualClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(
        self,
        *,
        seconds: float = 0,
        milliseconds: int = 0,
        days: int = 0,
    ) -> None:
        self.current += timedelta(days=days, seconds=seconds, milliseconds=milliseconds)


@dataclass(slots=True)
class _LockRecord:
    token: str
    expires_at: datetime


@dataclass(slots=True)
class _FakePostgresStorage:
    """In-memory PostgreSQL mailbox fake for protocol-level tests.

    The fake stores the same logical read models that a PostgreSQL schema would:
    serialized messages, exact-channel timelines, consumed/unconsumed state, and
    one compaction lock row per namespace.
    """

    clock: _ManualClock
    messages: dict[tuple[str, str], str] = field(default_factory=dict)
    timeline: dict[str, list[str]] = field(default_factory=dict)
    channel_timeline: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    unconsumed: dict[str, list[str]] = field(default_factory=dict)
    consumed: dict[str, list[str]] = field(default_factory=dict)
    consumed_info: dict[tuple[str, str], str] = field(default_factory=dict)
    locks: dict[str, _LockRecord] = field(default_factory=dict)
    renew_failure_namespaces: set[str] = field(default_factory=set)
    scan_calls: list[tuple[str, str]] = field(default_factory=list)

    async def append_messages(self, namespace: str, rows: list[Any]) -> None:
        for row in rows:
            key = (namespace, row.message.id)
            self.messages[key] = row.raw_message
            self._insert_sorted(self.timeline.setdefault(namespace, []), row.message.id)
            self._insert_sorted(
                self.channel_timeline.setdefault((namespace, row.message.channel), []),
                row.message.id,
            )
            self._insert_sorted(
                self.unconsumed.setdefault(namespace, []),
                row.message.id,
            )

    async def scan_message_ids(
        self,
        namespace: str,
        *,
        source: str,
        channel: str | None,
        after_id: str | None,
        cursor: str | None,
        order: str,
        limit: int | None,
    ) -> list[str]:
        self.scan_calls.append((namespace, source))
        ids = list(self._source_ids(namespace, source=source, channel=channel))
        ids.sort()

        if order == "oldest_first":
            start = cursor or after_id
            eligible = [
                message_id for message_id in ids if start is None or message_id > start
            ]
        else:
            eligible = [
                message_id
                for message_id in ids
                if (after_id is None or message_id > after_id)
                and (cursor is None or message_id < cursor)
            ]
            eligible.reverse()

        if limit is None:
            return eligible
        return eligible[:limit]

    async def load_messages(
        self,
        namespace: str,
        message_ids: list[str],
    ) -> list[str | None]:
        return [
            self.messages.get((namespace, message_id)) for message_id in message_ids
        ]

    async def load_consumed_infos(
        self,
        namespace: str,
        message_ids: list[str],
    ) -> list[str | None]:
        return [
            self.consumed_info.get((namespace, message_id))
            for message_id in message_ids
        ]

    async def consume_messages(
        self,
        namespace: str,
        *,
        message_ids: list[str],
        consumed_info_json: str,
        strict: bool,
    ) -> tuple[list[str], list[str], list[str]]:
        missing = [
            message_id
            for message_id in message_ids
            if (namespace, message_id) not in self.messages
        ]
        if strict and missing:
            return [], [], missing

        consumed: list[str] = []
        already_consumed: list[str] = []
        not_found: list[str] = []
        for message_id in message_ids:
            key = (namespace, message_id)
            if key not in self.messages:
                not_found.append(message_id)
                continue
            if key in self.consumed_info:
                already_consumed.append(message_id)
                continue

            self.consumed_info[key] = consumed_info_json
            self._remove_value(self.unconsumed.setdefault(namespace, []), message_id)
            self._insert_sorted(self.consumed.setdefault(namespace, []), message_id)
            consumed.append(message_id)

        return consumed, already_consumed, not_found

    async def is_unconsumed(self, namespace: str, message_id: str) -> bool:
        return message_id in self.unconsumed.get(namespace, [])

    async def delete_messages(self, namespace: str, messages: list[Any]) -> int:
        consumed_info_removed = 0
        for message in messages:
            key = (namespace, message.id)
            if key in self.consumed_info:
                consumed_info_removed += 1
                del self.consumed_info[key]
            self.messages.pop(key, None)
            self._remove_value(self.timeline.setdefault(namespace, []), message.id)
            self._remove_value(
                self.channel_timeline.setdefault((namespace, message.channel), []),
                message.id,
            )
            self._remove_value(self.unconsumed.setdefault(namespace, []), message.id)
            self._remove_value(self.consumed.setdefault(namespace, []), message.id)
        return consumed_info_removed

    async def try_acquire_compact_lock(
        self,
        namespace: str,
        token: str,
        *,
        ttl: timedelta,
        now: datetime,
    ) -> bool:
        existing = self.locks.get(namespace)
        if existing is not None and existing.expires_at > now:
            return False
        self.locks[namespace] = _LockRecord(token=token, expires_at=now + ttl)
        return True

    async def renew_compact_lock(
        self,
        namespace: str,
        token: str,
        *,
        ttl: timedelta,
        now: datetime,
    ) -> bool:
        if namespace in self.renew_failure_namespaces:
            raise RuntimeError("renew failed")
        existing = self.locks.get(namespace)
        if existing is None or existing.token != token:
            return False
        self.locks[namespace] = _LockRecord(token=token, expires_at=now + ttl)
        return True

    async def release_compact_lock(self, namespace: str, token: str) -> None:
        existing = self.locks.get(namespace)
        if existing is not None and existing.token == token:
            del self.locks[namespace]

    def lock_namespace(self, namespace: str, *, token: str = "other") -> None:
        self.locks[namespace] = _LockRecord(
            token=token,
            expires_at=self.clock.now() + timedelta(days=1),
        )

    def delete_message_row_only(self, namespace: str, message_id: str) -> None:
        self.messages.pop((namespace, message_id), None)

    def _source_ids(
        self,
        namespace: str,
        *,
        source: str,
        channel: str | None,
    ) -> list[str]:
        if source == "timeline":
            return self.timeline.get(namespace, [])
        if source == "unconsumed":
            return self.unconsumed.get(namespace, [])
        if source == "consumed":
            return self.consumed.get(namespace, [])
        if source == "channel":
            return self.channel_timeline.get((namespace, channel or ""), [])
        raise AssertionError(f"Unexpected source: {source}")

    @staticmethod
    def _insert_sorted(bucket: list[str], message_id: str) -> None:
        bucket.append(message_id)
        bucket.sort()

    @staticmethod
    def _remove_value(bucket: list[str], message_id: str) -> None:
        with suppress(ValueError):
            bucket.remove(message_id)


def _make_storage() -> tuple[_ManualClock, _FakePostgresStorage]:
    clock = _ManualClock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    return clock, _FakePostgresStorage(clock)


def _sqlmodel_message_record(
    serializer: JSONMessageSerializer,
    *,
    namespace: str,
    message: Message,
    consumed_by: str | None = None,
    consumed_at: datetime | None = None,
) -> Any:
    return mailbox_postgres_module._MailboxMessageRecord(
        namespace=namespace,
        message_id=message.id,
        channel=message.channel,
        created_at=message.created_at,
        producer=message.producer,
        raw_message=serializer.dump_message(message),
        raw_consumed_info=(
            None
            if consumed_at is None
            else serializer.dump_consumed_info(consumed_at, consumed_by=consumed_by)
        ),
    )


def _make_compiled_storage(
    responses: list[_BehaviorResult],
) -> tuple[SQLModelPostgresMailboxStorage, _CompiledExecuteSession]:
    session = _CompiledExecuteSession(responses=list(responses))
    storage = SQLModelPostgresMailboxStorage(
        object(),  # type: ignore[arg-type]
        _sessionmaker=_CompiledExecuteSessionMaker(session),  # type: ignore[arg-type]
    )
    return storage, session


@dataclass(slots=True)
class _FakeBeginContext:
    value: Any

    async def __aenter__(self) -> Any:
        return self.value

    async def __aexit__(self, *_: object) -> None:
        return None


@dataclass(slots=True)
class _RecordingConnection:
    callbacks: list[Any] = field(default_factory=list)

    async def run_sync(self, callback: Any) -> None:
        self.callbacks.append(callback)


@dataclass(slots=True)
class _RecordingEngine:
    connection: _RecordingConnection

    def begin(self) -> _FakeBeginContext:
        return _FakeBeginContext(self.connection)


@dataclass(slots=True)
class _RecordingSession:
    added: list[Any] = field(default_factory=list)

    def add_all(self, items: list[Any]) -> None:
        self.added.extend(items)


@dataclass(slots=True)
class _RecordingSessionMaker:
    session: _RecordingSession

    def begin(self) -> _FakeBeginContext:
        return _FakeBeginContext(self.session)


@dataclass(slots=True)
class _ScalarResult:
    value: str | None

    def scalar_one_or_none(self) -> str | None:
        return self.value


@dataclass(slots=True)
class _ExecuteRecordingSession:
    result_value: str | None
    statements: list[Any] = field(default_factory=list)

    async def execute(self, statement: Any) -> _ScalarResult:
        self.statements.append(statement)
        return _ScalarResult(self.result_value)


@dataclass(slots=True)
class _ExecuteRecordingSessionMaker:
    session: _ExecuteRecordingSession

    def begin(self) -> _FakeBeginContext:
        return _FakeBeginContext(self.session)


@dataclass(slots=True)
class _CompiledStatement:
    sql: str
    params: dict[str, Any]
    visit_name: str


@dataclass(slots=True)
class _BehaviorScalars:
    values: list[Any]

    def __iter__(self):
        return iter(self.values)


@dataclass(slots=True)
class _BehaviorResult:
    rows: list[Any]
    scalar_values: list[Any]

    def all(self) -> list[Any]:
        return list(self.rows)

    def scalars(self) -> _BehaviorScalars:
        return _BehaviorScalars(self.scalar_values)

    def scalar_one_or_none(self) -> Any:
        return self.scalar_values[0] if self.scalar_values else None


@dataclass(slots=True)
class _CompiledExecuteSession:
    responses: list[_BehaviorResult]
    statements: list[_CompiledStatement] = field(default_factory=list)

    async def execute(self, statement: Any) -> _BehaviorResult:
        compiled = statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"render_postcompile": True},
        )
        self.statements.append(
            _CompiledStatement(
                sql=str(compiled),
                params=dict(compiled.params),
                visit_name=statement.__visit_name__,
            )
        )
        return self.responses.pop(0)


@dataclass(slots=True)
class _CompiledExecuteSessionMaker:
    session: _CompiledExecuteSession

    def begin(self) -> _FakeBeginContext:
        return _FakeBeginContext(self.session)

    def __call__(self) -> _FakeBeginContext:
        return _FakeBeginContext(self.session)


def test_sqlmodel_storage_from_dsn_normalizes_to_async_psycopg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    fake_engine = object()

    def fake_create_async_engine(dsn: str, **kwargs: object) -> object:
        captured["dsn"] = dsn
        captured["kwargs"] = kwargs
        return fake_engine

    monkeypatch.setattr(
        mailbox_postgres_module, "create_async_engine", fake_create_async_engine
    )

    storage = SQLModelPostgresMailboxStorage.from_dsn(
        "postgresql://user:password@localhost:5432/kapybara",
        echo=True,
        connect_args={"sslmode": "require"},
    )

    assert (
        captured["dsn"] == "postgresql+psycopg://user:password@localhost:5432/kapybara"
    )
    assert captured["kwargs"] == {
        "echo": True,
        "connect_args": {"sslmode": "require"},
    }
    assert storage._engine is fake_engine


def test_sqlmodel_schema_uses_timezone_aware_datetimes() -> None:
    assert mailbox_postgres_module._MESSAGE_TABLE.c.created_at.type.timezone is True
    assert mailbox_postgres_module._LOCK_TABLE.c.expires_at.type.timezone is True


def test_compaction_lock_upsert_statement_is_atomic_and_expiry_aware() -> None:
    now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)
    statement = mailbox_postgres_module._acquire_compact_lock_statement(
        namespace="agent-a",
        token="token-a",
        expires_at=now + timedelta(minutes=5),
        now=now,
    )

    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )

    assert "ON CONFLICT (namespace) DO UPDATE" in compiled
    assert "mailbox_compaction_locks.expires_at <=" in compiled
    assert "RETURNING mailbox_compaction_locks.namespace" in compiled


@pytest.mark.anyio
async def test_sqlmodel_storage_create_schema_runs_sqlmodel_metadata() -> None:
    connection = _RecordingConnection()
    storage = SQLModelPostgresMailboxStorage(_RecordingEngine(connection))  # type: ignore[arg-type]

    await storage.create_schema()

    assert connection.callbacks == [SQLModel.metadata.create_all]


@pytest.mark.anyio
async def test_sqlmodel_storage_append_messages_builds_sqlmodel_records() -> None:
    recording_session = _RecordingSession()
    storage = SQLModelPostgresMailboxStorage(
        object(),  # type: ignore[arg-type]
        _sessionmaker=_RecordingSessionMaker(recording_session),  # type: ignore[arg-type]
    )
    clock, fake_storage = _make_storage()
    writer = PostgresMailboxWriter(fake_storage, clock=clock)
    messages = await writer.put_many(
        namespace="agent-a",
        items=[
            MessageInput(
                channel="telegram/chat/1", payload={"index": 1}, producer="demo"
            ),
            MessageInput(channel="telegram/chat/2", payload={"index": 2}),
        ],
        producer="fallback",
    )

    rows = [
        mailbox_postgres_module._StoredMessageRow(
            message=message,
            raw_message=fake_storage.messages[("agent-a", message.id)],
        )
        for message in messages
    ]

    await storage.append_messages("agent-a", rows)

    assert len(recording_session.added) == 2
    assert [record.namespace for record in recording_session.added] == [
        "agent-a",
        "agent-a",
    ]
    assert [record.message_id for record in recording_session.added] == [
        message.id for message in messages
    ]
    assert [record.channel for record in recording_session.added] == [
        "telegram/chat/1",
        "telegram/chat/2",
    ]
    assert [record.producer for record in recording_session.added] == [
        "demo",
        "fallback",
    ]


@pytest.mark.anyio
async def test_sqlmodel_storage_try_acquire_lock_returns_false_when_upsert_returns_no_row() -> (
    None
):
    session = _ExecuteRecordingSession(result_value=None)
    storage = SQLModelPostgresMailboxStorage(
        object(),  # type: ignore[arg-type]
        _sessionmaker=_ExecuteRecordingSessionMaker(session),  # type: ignore[arg-type]
    )

    acquired = await storage.try_acquire_compact_lock(
        "agent-a",
        "token-a",
        ttl=timedelta(minutes=5),
        now=datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC),
    )

    assert acquired is False
    assert len(session.statements) == 1


@pytest.mark.anyio
async def test_sqlmodel_storage_scan_and_load_support_ordering_and_filters() -> None:
    clock = _ManualClock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    serializer = JSONMessageSerializer()
    first = Message(
        id=str(uuid6.uuid7()),
        channel="telegram/chat/1",
        payload={"index": 1},
        created_at=clock.now(),
        producer="telegram",
    )
    clock.advance(milliseconds=1)
    second = Message(
        id=str(uuid6.uuid7()),
        channel="telegram/chat/1",
        payload={"index": 2},
        created_at=clock.now(),
        producer="telegram",
    )
    clock.advance(milliseconds=1)
    third = Message(
        id=str(uuid6.uuid7()),
        channel="telegram/chat/2",
        payload={"index": 3},
        created_at=clock.now(),
        producer="email",
    )
    consumed_at = clock.now()
    storage, session = _make_compiled_storage(
        [
            _BehaviorResult(rows=[], scalar_values=[first.id, second.id, third.id]),
            _BehaviorResult(rows=[], scalar_values=[third.id]),
            _BehaviorResult(rows=[], scalar_values=[first.id, second.id]),
            _BehaviorResult(rows=[], scalar_values=[second.id]),
            _BehaviorResult(rows=[], scalar_values=[first.id, third.id]),
            _BehaviorResult(
                rows=[
                    (second.id, serializer.dump_message(second)),
                    (first.id, serializer.dump_message(first)),
                ],
                scalar_values=[second.id, first.id],
            ),
            _BehaviorResult(
                rows=[
                    (
                        second.id,
                        serializer.dump_consumed_info(consumed_at, consumed_by="agent"),
                    )
                ],
                scalar_values=[second.id],
            ),
        ]
    )

    oldest = await storage.scan_message_ids(
        "agent-a",
        source="timeline",
        channel=None,
        after_id=None,
        cursor=None,
        order="oldest_first",
        limit=None,
    )
    newest = await storage.scan_message_ids(
        "agent-a",
        source="timeline",
        channel=None,
        after_id=first.id,
        cursor=None,
        order="newest_first",
        limit=1,
    )
    channel_ids = await storage.scan_message_ids(
        "agent-a",
        source="channel",
        channel="telegram/chat/1",
        after_id=None,
        cursor=None,
        order="oldest_first",
        limit=None,
    )
    consumed_ids = await storage.scan_message_ids(
        "agent-a",
        source="consumed",
        channel=None,
        after_id=None,
        cursor=None,
        order="oldest_first",
        limit=None,
    )
    unconsumed_ids = await storage.scan_message_ids(
        "agent-a",
        source="unconsumed",
        channel=None,
        after_id=None,
        cursor=None,
        order="oldest_first",
        limit=None,
    )
    raw_messages = await storage.load_messages(
        "agent-a", [second.id, first.id, "missing"]
    )
    raw_consumed_infos = await storage.load_consumed_infos(
        "agent-a",
        [first.id, second.id, "missing"],
    )

    assert oldest == [first.id, second.id, third.id]
    assert newest == [third.id]
    assert channel_ids == [first.id, second.id]
    assert consumed_ids == [second.id]
    assert unconsumed_ids == [first.id, third.id]
    assert raw_messages == [
        serializer.dump_message(second),
        serializer.dump_message(first),
        None,
    ]
    assert raw_consumed_infos == [
        None,
        serializer.dump_consumed_info(consumed_at, consumed_by="agent"),
        None,
    ]

    assert "FROM mailbox_messages" in session.statements[0].sql
    assert "ORDER BY mailbox_messages.message_id ASC" in session.statements[0].sql
    assert "agent-a" in session.statements[0].params.values()

    assert "ORDER BY mailbox_messages.message_id DESC" in session.statements[1].sql
    assert "LIMIT" in session.statements[1].sql
    assert first.id in session.statements[1].params.values()

    assert "mailbox_messages.channel" in session.statements[2].sql
    assert "telegram/chat/1" in session.statements[2].params.values()

    assert "raw_consumed_info IS NOT NULL" in session.statements[3].sql
    assert "raw_consumed_info IS NULL" in session.statements[4].sql

    assert "mailbox_messages.message_id IN" in session.statements[5].sql
    assert {second.id, first.id, "missing"}.issubset(
        set(session.statements[5].params.values())
    )

    assert "mailbox_messages.raw_consumed_info" in session.statements[6].sql
    assert {first.id, second.id, "missing"}.issubset(
        set(session.statements[6].params.values())
    )


@pytest.mark.anyio
async def test_sqlmodel_storage_consume_messages_updates_state_and_classifies_ids() -> (
    None
):
    clock = _ManualClock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    serializer = JSONMessageSerializer()
    first = Message(
        id=str(uuid6.uuid7()),
        channel="telegram/chat/1",
        payload={"index": 1},
        created_at=clock.now(),
        producer="telegram",
    )
    clock.advance(milliseconds=1)
    second = Message(
        id=str(uuid6.uuid7()),
        channel="telegram/chat/1",
        payload={"index": 2},
        created_at=clock.now(),
        producer="telegram",
    )
    previous_consumed_at = clock.now()
    first_row = _sqlmodel_message_record(
        serializer,
        namespace="agent-a",
        message=first,
    )
    second_row = _sqlmodel_message_record(
        serializer,
        namespace="agent-a",
        message=second,
        consumed_by="agent",
        consumed_at=previous_consumed_at,
    )
    storage, session = _make_compiled_storage(
        [
            _BehaviorResult(
                rows=[first_row, second_row], scalar_values=[first_row, second_row]
            ),
            _BehaviorResult(rows=[], scalar_values=[]),
        ]
    )

    consumed_info_json = serializer.dump_consumed_info(
        clock.now(),
        consumed_by="agent-2",
    )
    consumed, already_consumed, not_found = await storage.consume_messages(
        "agent-a",
        message_ids=[first.id, second.id, "missing"],
        consumed_info_json=consumed_info_json,
        strict=False,
    )
    strict_consumed, strict_already, strict_missing = await storage.consume_messages(
        "agent-a",
        message_ids=["unknown"],
        consumed_info_json=consumed_info_json,
        strict=True,
    )

    assert consumed == [first.id]
    assert already_consumed == [second.id]
    assert not_found == ["missing"]
    assert strict_consumed == []
    assert strict_already == []
    assert strict_missing == ["unknown"]
    assert first_row.raw_consumed_info == consumed_info_json
    assert second_row.raw_consumed_info == serializer.dump_consumed_info(
        previous_consumed_at,
        consumed_by="agent",
    )
    assert "FOR UPDATE" in session.statements[0].sql
    assert "agent-a" in session.statements[0].params.values()
    assert {first.id, second.id, "missing"}.issubset(
        set(session.statements[0].params.values())
    )


@pytest.mark.anyio
async def test_sqlmodel_storage_delete_messages_removes_rows_and_counts_consumed_info() -> (
    None
):
    clock = _ManualClock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    serializer = JSONMessageSerializer()
    first = Message(
        id=str(uuid6.uuid7()),
        channel="telegram/chat/1",
        payload={"index": 1},
        created_at=clock.now(),
    )
    clock.advance(milliseconds=1)
    second = Message(
        id=str(uuid6.uuid7()),
        channel="telegram/chat/2",
        payload={"index": 2},
        created_at=clock.now(),
    )
    consumed_info_json = serializer.dump_consumed_info(clock.now(), consumed_by="agent")
    storage, session = _make_compiled_storage(
        [
            _BehaviorResult(rows=[], scalar_values=[None, consumed_info_json]),
            _BehaviorResult(rows=[], scalar_values=[]),
        ]
    )

    consumed_info_removed = await storage.delete_messages("agent-a", [first, second])

    assert consumed_info_removed == 1
    assert session.statements[0].visit_name == "select"
    assert "raw_consumed_info" in session.statements[0].sql
    assert session.statements[1].visit_name == "delete"
    assert "DELETE FROM mailbox_messages" in session.statements[1].sql
    assert "agent-a" in session.statements[1].params.values()
    assert {first.id, second.id}.issubset(set(session.statements[1].params.values()))


@pytest.mark.anyio
async def test_sqlmodel_storage_is_unconsumed_distinguishes_states_and_namespace() -> (
    None
):
    clock = _ManualClock(datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC))
    second = Message(
        id=str(uuid6.uuid7()),
        channel="telegram/chat/1",
        payload={"index": 2},
        created_at=clock.now(),
    )
    storage, session = _make_compiled_storage(
        [
            _BehaviorResult(rows=[], scalar_values=["present"]),
            _BehaviorResult(rows=[], scalar_values=[]),
            _BehaviorResult(rows=[], scalar_values=["present"]),
            _BehaviorResult(rows=[], scalar_values=[]),
        ]
    )

    assert await storage.is_unconsumed("agent-a", "first") is True
    assert await storage.is_unconsumed("agent-a", second.id) is False
    assert await storage.is_unconsumed("agent-b", second.id) is True
    assert await storage.is_unconsumed("agent-a", "missing") is False

    for compiled in session.statements:
        assert "raw_consumed_info IS NULL" in compiled.sql
        assert "LIMIT" in compiled.sql
    assert "agent-a" in session.statements[0].params.values()
    assert second.id in session.statements[1].params.values()
    assert "agent-b" in session.statements[2].params.values()
    assert "missing" in session.statements[3].params.values()


@pytest.mark.anyio
async def test_sqlmodel_storage_lock_lifecycle_success_paths() -> None:
    session = _ExecuteRecordingSession(result_value="agent-a")
    storage = SQLModelPostgresMailboxStorage(
        object(),  # type: ignore[arg-type]
        _sessionmaker=_ExecuteRecordingSessionMaker(session),  # type: ignore[arg-type]
    )
    now = datetime(2026, 5, 21, 12, 0, 0, tzinfo=UTC)

    assert (
        await storage.try_acquire_compact_lock(
            "agent-a",
            "token-a",
            ttl=timedelta(minutes=5),
            now=now,
        )
        is True
    )

    lock_row = mailbox_postgres_module._MailboxCompactionLockRecord(
        namespace="agent-a",
        token="token-a",
        expires_at=now + timedelta(minutes=5),
    )
    delete_calls: list[Any] = []

    @dataclass(slots=True)
    class _LockSession:
        async def delete(self, row: Any) -> None:
            delete_calls.append(row)

    lock_session = _LockSession()
    lock_storage = SQLModelPostgresMailboxStorage(
        object(),  # type: ignore[arg-type]
        _sessionmaker=_ExecuteRecordingSessionMaker(lock_session),  # type: ignore[arg-type]
    )

    async def load_lock(_session: Any, namespace: str):
        assert namespace == "agent-a"
        return lock_row

    lock_storage._load_lock_for_update = load_lock  # type: ignore[method-assign]
    renewed = await lock_storage.renew_compact_lock(
        "agent-a",
        "token-a",
        ttl=timedelta(minutes=10),
        now=now,
    )
    await lock_storage.release_compact_lock("agent-a", "token-a")

    assert renewed is True
    assert lock_row.expires_at == now + timedelta(minutes=10)
    assert delete_calls == [lock_row]


@pytest.mark.anyio
async def test_writer_put_requires_explicit_namespace_and_writes_there() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)

    message = await writer.put(
        namespace="agent-a",
        channel="telegram/chat/1",
        payload={"index": 1},
        producer="telegram",
    )

    with pytest.raises(TypeError):
        await writer.put(channel="telegram/chat/1", payload={"index": 2})  # type: ignore[call-arg]

    assert (
        inspect.signature(PostgresMailboxWriter.put).parameters["namespace"].default
        is inspect.Signature.empty
    )
    assert [stored.id for stored in await inbox.get(limit=None)] == [message.id]


@pytest.mark.anyio
async def test_writer_can_route_to_multiple_namespaces() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    inbox_a = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)
    inbox_b = PostgresMailboxInbox(storage, namespace="agent-b", clock=clock)

    first = await writer.put(
        namespace="agent-a",
        channel="telegram/chat/1",
        payload={"namespace": "a"},
    )
    clock.advance(milliseconds=1)
    second = await writer.put(
        namespace="agent-b",
        channel="telegram/chat/1",
        payload={"namespace": "b"},
    )

    assert [message.id for message in await inbox_a.get(limit=None)] == [first.id]
    assert [message.id for message in await inbox_b.get(limit=None)] == [second.id]


@pytest.mark.anyio
async def test_writer_put_many_preserves_order_and_producer_fallback() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)

    messages = await writer.put_many(
        namespace="agent-a",
        items=[
            MessageInput(channel="telegram/chat/1", payload={"index": 0}),
            MessageInput(
                channel="telegram/chat/1",
                payload={"index": 1},
                producer="email",
            ),
            MessageInput(channel="telegram/chat/2", payload={"index": 2}),
        ],
        producer="telegram",
    )

    assert [message.payload["index"] for message in messages] == [0, 1, 2]
    assert [message.producer for message in messages] == [
        "telegram",
        "email",
        "telegram",
    ]
    assert [message.id for message in await inbox.get(limit=None)] == [
        message.id for message in messages
    ]


@pytest.mark.anyio
async def test_writer_allow_list_allows_explicit_namespace() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(
        storage,
        clock=clock,
        allowed_namespaces={"agent-a", "agent-b"},
    )
    inbox = PostgresMailboxInbox(storage, namespace="agent-b", clock=clock)

    message = await writer.put(
        namespace="agent-b",
        channel="telegram/chat/1",
        payload={"index": 1},
    )

    assert [stored.id for stored in await inbox.get(limit=None)] == [message.id]


@pytest.mark.anyio
async def test_writer_allow_list_rejects_disallowed_namespace_without_writing() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(
        storage,
        clock=clock,
        allowed_namespaces={"agent-a"},
    )

    with pytest.raises(NamespaceNotAllowedError):
        await writer.put(
            namespace="agent-b",
            channel="telegram/chat/1",
            payload={"index": 1},
        )

    assert storage.messages == {}
    assert storage.timeline == {}


@pytest.mark.anyio
async def test_inbox_get_supports_exact_channel_and_consumed_filters() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)

    root = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"kind": "root"}
    )
    clock.advance(milliseconds=1)
    sibling = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"kind": "sibling"}
    )
    clock.advance(milliseconds=1)
    child = await writer.put(
        namespace="agent-a",
        channel="telegram/chat/1/thread/2",
        payload={"kind": "child"},
    )
    clock.advance(milliseconds=1)
    other = await writer.put(
        namespace="agent-a", channel="telegram/chat/2", payload={"kind": "other"}
    )
    _ = other

    exact = await inbox.get(MessageFilter(channel="telegram/chat/1"))
    await inbox.consume([sibling.id, child.id], consumer="agent")
    unconsumed = await inbox.get(MessageFilter(consumed=False), limit=None)
    consumed = await inbox.get(MessageFilter(consumed=True), limit=None)
    exact_unconsumed = await inbox.get(
        MessageFilter(channel="telegram/chat/1", consumed=False),
        limit=None,
    )
    exact_consumed = await inbox.get(
        MessageFilter(channel="telegram/chat/1", consumed=True),
        limit=None,
    )

    assert [message.id for message in exact] == [root.id, sibling.id]
    assert [message.id for message in unconsumed] == [root.id, other.id]
    assert [message.id for message in consumed] == [sibling.id, child.id]
    assert [message.id for message in exact_unconsumed] == [root.id]
    assert [message.id for message in exact_consumed] == [sibling.id]


@pytest.mark.anyio
async def test_consume_reports_consumed_already_consumed_and_not_found() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)
    first = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 1}
    )
    second = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 2}
    )
    missing_id = str(uuid6.uuid7())

    first_result = await inbox.consume([first.id], consumer="agent")
    second_result = await inbox.consume(
        [first.id, second.id, missing_id], consumer="agent"
    )

    assert first_result.consumed == [first.id]
    assert second_result.consumed == [second.id]
    assert second_result.already_consumed == [first.id]
    assert second_result.not_found == [missing_id]


@pytest.mark.anyio
async def test_consume_strict_raises_for_unknown_ids() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)
    message = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 1}
    )
    missing_id = str(uuid6.uuid7())

    with pytest.raises(UnknownMessageError) as exc_info:
        await inbox.consume([message.id, missing_id], strict=True)

    assert exc_info.value.message_ids == [missing_id]
    assert ("agent-a", message.id) not in storage.consumed_info


@pytest.mark.anyio
async def test_message_filter_supports_producer_since_until_and_exact_channel() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)

    await writer.put(
        namespace="agent-a",
        channel="telegram/chat/1",
        payload={"index": 0},
        producer="telegram",
    )
    clock.advance(seconds=1)
    kept = await writer.put(
        namespace="agent-a",
        channel="telegram/chat/1",
        payload={"index": 1},
        producer="telegram",
    )
    clock.advance(seconds=1)
    await writer.put(
        namespace="agent-a",
        channel="telegram/chat/1",
        payload={"index": 2},
        producer="telegram",
    )
    clock.advance(milliseconds=1)
    await writer.put(
        namespace="agent-a",
        channel="telegram/chat/1/thread/2",
        payload={"index": 3},
        producer="telegram",
    )
    clock.advance(milliseconds=1)
    await writer.put(
        namespace="agent-a",
        channel="telegram/chat/1",
        payload={"index": 4},
        producer="email",
    )

    messages = await inbox.get(
        MessageFilter(
            channel="telegram/chat/1",
            producer="telegram",
            since=kept.created_at,
            until=kept.created_at,
        ),
        limit=None,
    )

    assert [message.id for message in messages] == [kept.id]


@pytest.mark.anyio
async def test_writer_rejects_non_json_payloads_by_default() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)

    with pytest.raises(PayloadSerializationError):
        await writer.put(
            namespace="agent-a",
            channel="telegram/chat/1",
            payload=object(),  # type: ignore[arg-type]
        )


@pytest.mark.anyio
async def test_get_after_missing_id_uses_pure_cursor_semantics() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)
    first = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 1}
    )
    clock.advance(milliseconds=1)
    second = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 2}
    )
    missing_id = str(uuid6.UUID(int=uuid6.UUID(first.id).int + 1, version=7))

    received = await inbox.get(after_id=missing_id, limit=None)

    assert [message.id for message in received] == [second.id]


@pytest.mark.anyio
async def test_get_after_invalid_uuid_raises_window_error() -> None:
    clock, storage = _make_storage()
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)

    with pytest.raises(InvalidMessageWindowError):
        await inbox.get(after_id="not-a-uuid")


@pytest.mark.anyio
async def test_get_skips_missing_message_rows_but_keeps_cursor_semantics() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)
    first = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 1}
    )
    clock.advance(milliseconds=1)
    second = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 2}
    )
    clock.advance(milliseconds=1)
    third = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 3}
    )

    storage.delete_message_row_only("agent-a", first.id)

    received = await inbox.get(after_id=first.id, limit=10)

    assert [message.id for message in received] == [second.id, third.id]


@pytest.mark.anyio
async def test_created_at_is_derived_from_generated_uuid_timestamp() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    clock.current = datetime(2026, 5, 21, 13, 14, 15, 123000, tzinfo=UTC)

    message = await writer.put(
        namespace="agent-a",
        channel="telegram/chat/1",
        payload={"index": 1},
    )

    assert message.created_at == datetime.fromtimestamp(
        uuid6.UUID(message.id).time / 1000,
        tz=UTC,
    )


@pytest.mark.anyio
async def test_manual_compact_only_affects_requested_namespace() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    maintenance = PostgresMailboxMaintenance(storage, clock=clock)
    inbox_a = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)
    inbox_b = PostgresMailboxInbox(storage, namespace="agent-b", clock=clock)

    first = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 1}
    )
    await writer.put(
        namespace="agent-b", channel="telegram/chat/1", payload={"index": 2}
    )
    clock.advance(days=4)

    result = await maintenance.compact(
        namespace="agent-a",
        retention=RetentionPolicy(max_age=timedelta(days=1), keep_unconsumed=False),
    )

    assert result.messages_deleted == 1
    assert result.index_entries_removed == 4
    assert await inbox_a.get(limit=None) == []
    assert [message.payload["index"] for message in await inbox_b.get(limit=None)] == [
        2
    ]
    assert ("agent-a", first.id) not in storage.messages


@pytest.mark.anyio
async def test_compact_respects_keep_unconsumed_flag() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)
    maintenance = PostgresMailboxMaintenance(storage, clock=clock)

    old_unconsumed = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 1}
    )
    clock.advance(days=1, milliseconds=1)
    old_consumed = await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 2}
    )
    await inbox.consume([old_consumed.id], consumer="agent")
    clock.advance(days=4)

    result = await maintenance.compact(
        namespace="agent-a",
        retention=RetentionPolicy(max_age=timedelta(days=3), keep_unconsumed=True),
    )

    assert result.messages_deleted == 1
    assert result.skipped_unconsumed == 1
    assert [message.id for message in await inbox.get(limit=None)] == [
        old_unconsumed.id
    ]


@pytest.mark.anyio
async def test_auto_compacting_uses_explicit_namespaces_and_stops_on_exit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    maintenance = PostgresMailboxMaintenance(storage, clock=clock)
    inbox_a = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)
    inbox_b = PostgresMailboxInbox(storage, namespace="agent-b", clock=clock)
    inbox_c = PostgresMailboxInbox(storage, namespace="agent-c", clock=clock)

    await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 1}
    )
    await writer.put(
        namespace="agent-b", channel="telegram/chat/1", payload={"index": 2}
    )
    await writer.put(
        namespace="agent-c", channel="telegram/chat/1", payload={"index": 3}
    )
    clock.advance(days=4)
    caplog.set_level("WARNING")

    async with maintenance.auto_compacting(
        namespaces=["agent-a", "agent-b"],
        retention_provider=lambda namespace: {
            "agent-a": RetentionPolicy(max_age=timedelta(days=1)),
        }.get(namespace),
        interval=timedelta(milliseconds=10),
        jitter_ratio=0.0,
        use_postgres_lock=False,
    ):
        await asyncio.sleep(0.05)

    compaction_scan_calls = list(storage.scan_calls)
    storage.scan_calls.clear()

    assert await inbox_a.get(limit=None) == []
    assert [message.payload["index"] for message in await inbox_b.get(limit=None)] == [
        2
    ]
    assert [message.payload["index"] for message in await inbox_c.get(limit=None)] == [
        3
    ]
    assert not any(namespace == "agent-c" for namespace, _ in compaction_scan_calls)
    assert "Skipping mailbox compaction for namespace agent-b" in caplog.text

    await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 4}
    )
    clock.advance(days=4)
    await asyncio.sleep(0.05)
    assert [message.payload["index"] for message in await inbox_a.get(limit=None)] == [
        4
    ]


@pytest.mark.anyio
async def test_auto_compaction_lock_conflict_does_not_block_other_namespaces() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    maintenance = PostgresMailboxMaintenance(storage, clock=clock)
    inbox_a = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)
    inbox_b = PostgresMailboxInbox(storage, namespace="agent-b", clock=clock)

    await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 1}
    )
    await writer.put(
        namespace="agent-b", channel="telegram/chat/1", payload={"index": 2}
    )
    clock.advance(days=4)
    storage.lock_namespace("agent-a")

    async with maintenance.auto_compacting(
        namespaces=["agent-a", "agent-b"],
        retention_provider=lambda _: RetentionPolicy(max_age=timedelta(days=1)),
        interval=timedelta(milliseconds=10),
        jitter_ratio=0.0,
        use_postgres_lock=True,
    ):
        await asyncio.sleep(0.05)

    assert [message.payload["index"] for message in await inbox_a.get(limit=None)] == [
        1
    ]
    assert await inbox_b.get(limit=None) == []


@pytest.mark.anyio
async def test_run_auto_compact_forever_blocks_current_task_until_cancelled() -> None:
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    maintenance = PostgresMailboxMaintenance(storage, clock=clock)
    inbox = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)

    await writer.put(
        namespace="agent-a", channel="telegram/chat/1", payload={"index": 1}
    )
    clock.advance(days=4)

    task = asyncio.create_task(
        maintenance.run_auto_compact_forever(
            namespaces=["agent-a"],
            retention_provider=lambda _: RetentionPolicy(max_age=timedelta(days=1)),
            interval=timedelta(milliseconds=10),
            jitter_ratio=0.0,
            use_postgres_lock=False,
        )
    )

    for _ in range(100):
        if await inbox.get(limit=None) == []:
            break
        await asyncio.sleep(0.01)

    assert await inbox.get(limit=None) == []
    assert not task.done()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.anyio
async def test_auto_compaction_entry_points_share_internal_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock, storage = _make_storage()
    maintenance = PostgresMailboxMaintenance(storage, clock=clock)
    calls: list[bool] = []

    async def fake_loop(
        *,
        namespaces: tuple[str, ...],
        retention_provider: Any,
        interval: timedelta,
        use_postgres_lock: bool,
        lock_ttl: timedelta,
        lock_renew_interval: timedelta,
        jitter_ratio: float,
        logger: Any,
        stop_event: asyncio.Event | None,
    ) -> None:
        _ = (
            namespaces,
            retention_provider,
            interval,
            use_postgres_lock,
            lock_ttl,
            lock_renew_interval,
            jitter_ratio,
            logger,
        )
        calls.append(stop_event is None)
        if stop_event is None:
            raise asyncio.CancelledError
        await stop_event.wait()

    monkeypatch.setattr(maintenance, "_run_auto_compact_loop", fake_loop)

    async with maintenance.auto_compacting(
        namespaces=["agent-a"],
        retention_provider=lambda _: RetentionPolicy(),
        interval=timedelta(milliseconds=10),
    ):
        await asyncio.sleep(0)

    task = asyncio.create_task(
        maintenance.run_auto_compact_forever(
            namespaces=["agent-a"],
            retention_provider=lambda _: RetentionPolicy(),
            interval=timedelta(milliseconds=10),
        )
    )
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert calls == [False, True]


@pytest.mark.anyio
async def test_producer_supervisor_restarts_failed_producer_and_routes_multiple_namespaces() -> (
    None
):
    clock, storage = _make_storage()
    writer = PostgresMailboxWriter(storage, clock=clock)
    supervisor = MailboxProducerSupervisor(
        writer,
        restart_policy=RestartPolicy(
            initial_delay_seconds=0.01,
            max_delay_seconds=0.02,
            multiplier=1.0,
            jitter_ratio=0.0,
        ),
    )
    inbox_a = PostgresMailboxInbox(storage, namespace="agent-a", clock=clock)
    inbox_b = PostgresMailboxInbox(storage, namespace="agent-b", clock=clock)
    attempts: list[int] = []

    async def producer(writer: Any, token: Any) -> None:
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        await writer.put(
            namespace="agent-a",
            channel="telegram/chat/1",
            payload={"namespace": "a"},
            producer="demo",
        )
        await writer.put(
            namespace="agent-b",
            channel="telegram/chat/1",
            payload={"namespace": "b"},
            producer="demo",
        )
        await token.wait_cancelled()

    supervisor.register_producer("demo", producer)
    await supervisor.start()

    for _ in range(100):
        if (
            len(await inbox_a.get(limit=None)) == 1
            and len(await inbox_b.get(limit=None)) == 1
        ):
            break
        await asyncio.sleep(0.01)

    status_before_stop = supervisor.status("demo")
    await supervisor.stop(timeout=0.5)
    status_after_stop = supervisor.status("demo")

    assert len(attempts) >= 2
    assert [
        message.payload["namespace"] for message in await inbox_a.get(limit=None)
    ] == ["a"]
    assert [
        message.payload["namespace"] for message in await inbox_b.get(limit=None)
    ] == ["b"]
    assert status_before_stop.restart_count >= 1
    assert status_before_stop.state in {"running", "backoff", "stopping"}
    assert status_after_stop.state == "stopped"


def test_public_mailbox_api_is_split_and_old_surface_is_removed() -> None:
    writer = PostgresMailboxWriter(
        _FakePostgresStorage(_ManualClock(datetime.now(UTC)))
    )
    inbox = PostgresMailboxInbox(
        _FakePostgresStorage(_ManualClock(datetime.now(UTC))),
        namespace="agent-a",
    )
    maintenance = PostgresMailboxMaintenance(
        _FakePostgresStorage(_ManualClock(datetime.now(UTC)))
    )

    assert hasattr(kapy_mailbox, "PostgresMailboxWriter")
    assert hasattr(kapy_mailbox, "PostgresMailboxInbox")
    assert hasattr(kapy_mailbox, "PostgresMailboxMaintenance")
    assert hasattr(kapy_mailbox, "SQLModelPostgresMailboxStorage")
    assert not hasattr(kapy_mailbox, "RedisMailbox")
    assert not hasattr(kapy_mailbox, "MailboxWriter")
    assert not hasattr(kapy_mailbox, "MailboxAutoCompactor")
    assert not hasattr(kapy_mailbox, "AutoCompactorClosedError")
    assert not hasattr(writer, "get")
    assert not hasattr(writer, "consume")
    assert not hasattr(writer, "compact")
    assert not hasattr(inbox, "put")
    assert not hasattr(inbox, "put_many")
    assert not hasattr(inbox, "writer")
    assert not hasattr(inbox, "compact")
    assert not hasattr(maintenance, "get")
    assert not hasattr(maintenance, "consume")
    assert not hasattr(maintenance, "start_auto_compact")
    assert not hasattr(maintenance, "stop_auto_compact")

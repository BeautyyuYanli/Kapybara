"""Single-controller durable session coordination with advisory Valkey wakeups."""

import asyncio
import json
import logging
import math
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, Literal, LiteralString, cast
from uuid import UUID, uuid4

import psycopg
from psycopg.rows import tuple_row
from psycopg.types.json import Jsonb
from valkey.asyncio import Valkey
from valkey.exceptions import ValkeyError

from .contracts import (
    CheckpointWrite,
    Completion,
    Conflict,
    CreatedSession,
    Cursor,
    EventReceipt,
    HistoryExportPage,
    InputMode,
    InvalidArgument,
    JsonObject,
    JsonValue,
    NotFound,
    OutputDelta,
    QueryLimitExceeded,
    QueryResult,
    Record,
    RecordPage,
    RunFailure,
    RunnerState,
    RunResult,
    ServiceUnavailable,
    SessionInput,
    SessionPage,
    SessionRunner,
    SessionSpec,
    SessionView,
    Submission,
    SubmissionStatus,
)
from .encoding import (
    CHECKPOINT_BYTES,
    DELTA_BYTES,
    PAGE_BYTES,
    bounded,
    cursor,
    encode,
    fingerprint,
    page_limit,
    plain,
    sequence,
)
from .history import HISTORY_KINDS, compile_query, normalized, search_document, search_terms
from .store import Connection, Store

logger = logging.getLogger(__name__)
_SESSION_COLUMNS: LiteralString = (
    "id,title,machine_ids,default_machine_id,config,status,latest_run_id,"
    "next_seq,created_at,updated_at"
)
_RECORD_COLUMNS: LiteralString = (
    "session_id,seq,run_id,attempt,message_id,kind,data,text,created_at"
)


def _state(value: dict[str, Any]) -> RunnerState:
    return RunnerState(codec=value["codec"], data=value["data"])


def _view(row: dict[str, Any]) -> SessionView:
    return SessionView(
        id=row["id"],
        title=row["title"],
        machine_ids=tuple(row["machine_ids"]),
        default_machine_id=row["default_machine_id"],
        config=row["config"],
        status=row["status"],
        run_id=row["latest_run_id"],
        cursor=cursor(row["id"], row["next_seq"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _saved_view(row: dict[str, Any]) -> SessionView:
    return SessionView(
        id=UUID(row["id"]),
        title=row["title"],
        machine_ids=tuple(row["machine_ids"]),
        default_machine_id=row["default_machine_id"],
        config=row["config"],
        status=row["status"],
        run_id=UUID(row["run_id"]) if row["run_id"] else None,
        cursor=row["cursor"],
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _submission(row: dict[str, Any]) -> Submission:
    return Submission(
        request_id=UUID(row["request_id"]),
        session_id=UUID(row["session_id"]),
        input_id=UUID(row["input_id"]) if row["input_id"] else None,
        waiting_id=UUID(row["waiting_id"]),
    )


def _input(row: dict[str, Any]) -> SessionInput:
    return SessionInput(row["id"], row["seq"], row["mode"], row["payload"], row["event_id"])


def _record_view(row: dict[str, Any]) -> Record:
    return Record(
        cursor(row["session_id"], row["seq"]),
        row["run_id"],
        row["attempt"],
        row["message_id"],
        row["kind"],
        row["data"],
        row["text"],
        row["created_at"],
    )


def _input_text(payload: JsonValue) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        if payload.get("type") == "event" and "payload" in payload:
            return _input_text(payload["payload"])
        if payload.get("type") == "session.waiting" and isinstance(payload.get("output"), str):
            return cast(str, payload["output"])
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


def _check_record(
    session_id: UUID,
    kind: str,
    data: JsonValue,
    text: str,
    run_id: UUID | None = None,
    attempt: int | None = None,
    message_id: UUID | None = None,
) -> None:
    prototype = Record(
        cursor(session_id, 2**63 - 1),
        run_id,
        attempt,
        message_id,
        kind,
        data,
        text,
        datetime.now(UTC),
    )
    bounded(HistoryExportPage((prototype,), prototype.cursor, prototype.cursor, False), PAGE_BYTES)


def _mode(mode: InputMode) -> None:
    if mode not in ("steer", "queue"):
        raise InvalidArgument("mode must be steer or queue")


def _wait(seconds: float) -> None:
    if not math.isfinite(seconds) or not 0 <= seconds <= 30:
        raise InvalidArgument("wait_seconds must be between 0 and 30")


class SessionService:
    def __init__(
        self,
        *,
        database_url: str,
        valkey_url: str,
        runner: SessionRunner,
        schema: str = "kapy_state",
        namespace: str = "kapy_state",
    ) -> None:
        if not namespace or len(namespace) > 128:
            raise InvalidArgument("namespace must contain 1 to 128 characters")
        self._store = Store(database_url, schema)
        self._runner = runner
        self._valkey = Valkey.from_url(valkey_url, socket_connect_timeout=1, socket_timeout=1)
        self._channel = namespace + ":wake"
        self._pubsub = self._valkey.pubsub()
        self._changed = asyncio.Event()
        self._notify = asyncio.Event()
        self._tasks: dict[UUID, asyncio.Task[None]] = {}
        self._background: list[asyncio.Task[None]] = []
        self._available = False
        self._entered = False

    async def __aenter__(self) -> SessionService:
        if self._entered:
            raise Conflict("a SessionService instance can only be entered once")
        self._entered = True
        try:
            await self._store.open()
            self._available = True
            self._background = [
                asyncio.create_task(fn())
                for fn in (
                    self._coordinate,
                    self._listen,
                    self._publish_hints,
                )
            ]
            self._signal()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._available = False
        self._local_signal()
        tasks = [*self._background, *self._tasks.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._tasks.clear()
        self._background.clear()
        try:
            await self._pubsub.aclose()
        finally:
            try:
                await self._valkey.aclose()
            finally:
                await self._store.close()

    def _ensure_open(self) -> None:
        if not self._available:
            raise ServiceUnavailable("State service is not running")

    def _local_signal(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()

    def _signal(self) -> None:
        self._local_signal()
        self._notify.set()

    async def _pause(self, event: asyncio.Event, seconds: float = 1) -> None:
        try:
            async with asyncio.timeout(seconds):
                await event.wait()
        except TimeoutError:
            pass

    async def _publish_hints(self) -> None:
        while True:
            await self._notify.wait()
            self._notify.clear()
            try:
                await self._valkey.publish(self._channel, "changed")
            except ValkeyError, OSError:
                # The committed DB work and local wakeup have already succeeded.
                pass

    async def _listen(self) -> None:
        while True:
            try:
                await self._pubsub.subscribe(self._channel)
                while True:
                    message = await self._pubsub.get_message(
                        ignore_subscribe_messages=True, timeout=1
                    )
                    if message:
                        self._local_signal()
            except ValkeyError, OSError:
                await self._pubsub.aclose()
                self._pubsub = self._valkey.pubsub()
                await asyncio.sleep(1)

    async def _coordinate(self) -> None:
        try:
            while True:
                changed = self._changed
                async with asyncio.timeout(2):
                    await self._store.check_lease()
                async with self._store.pool.connection() as conn:
                    rows = await (
                        await conn.execute(
                            "SELECT id FROM sessions s WHERE status IN ('running','deleting') OR "
                            "EXISTS (SELECT 1 FROM inputs i WHERE i.session_id=s.id AND "
                            "i.state='pending')"
                        )
                    ).fetchall()
                for row in rows:
                    session_id = row["id"]
                    if session_id not in self._tasks:
                        task = asyncio.create_task(self._run_session(session_id))
                        self._tasks[session_id] = task
                        task.add_done_callback(lambda task, sid=session_id: self._done(sid, task))
                await self._pause(changed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("State coordination stopped: %s", type(exc).__name__)
            self._available = False
            self._local_signal()
            for task in self._tasks.values():
                task.cancel()

    def _done(self, session_id: UUID, task: asyncio.Task[None]) -> None:
        if self._tasks.get(session_id) is task:
            self._tasks.pop(session_id, None)
        if not task.cancelled() and task.exception():
            logger.error("State runner persistence stopped: %s", type(task.exception()).__name__)
            # Retry scanning the durable run, but never keep operating after losing the lease.
        self._local_signal()

    async def _session(self, conn: Connection, session_id: UUID) -> dict[str, Any]:
        row = await (
            await conn.execute(
                "SELECT " + _SESSION_COLUMNS + " FROM sessions WHERE id=%s", (session_id,)
            )
        ).fetchone()
        if not row:
            raise NotFound("session does not exist")
        return row

    async def _request(
        self,
        conn: Connection,
        request_id: UUID,
        operation: str,
        value: object,
    ) -> tuple[dict[str, Any] | None, str]:
        digest = fingerprint(value)
        row = await (
            await conn.execute("SELECT * FROM requests WHERE id=%s", (request_id,))
        ).fetchone()
        if row and (row["operation"] != operation or row["fingerprint"] != digest):
            raise Conflict("request_id was used with different parameters")
        return row, digest

    async def _append(
        self,
        conn: Connection,
        session_id: UUID,
        kind: str,
        data: JsonValue,
        text: str = "",
        *,
        run_id: UUID | None = None,
        attempt: int | None = None,
        message_id: UUID | None = None,
        emission_id: UUID | None = None,
        emission_fingerprint: str | None = None,
    ) -> int:
        # A full record must fit one page, including JSON escaping and cursor metadata.
        _check_record(session_id, kind, data, text, run_id, attempt, message_id)
        row = await (
            await conn.execute(
                "UPDATE sessions SET next_seq=next_seq+1,updated_at=now() WHERE id=%s "
                "RETURNING next_seq",
                (session_id,),
            )
        ).fetchone()
        if not row:
            raise NotFound("session does not exist")
        seq = row["next_seq"]
        await conn.execute(
            "INSERT INTO records(session_id,seq,run_id,attempt,message_id,kind,data,text,"
            "normalized_text,search_vector,emission_id,emission_fingerprint) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,to_tsvector('pg_catalog.simple',%s),%s,%s)",
            (
                session_id,
                seq,
                run_id,
                attempt,
                message_id,
                kind,
                Jsonb(data),
                text,
                normalized(text),
                search_document(text),
                emission_id,
                emission_fingerprint,
            ),
        )
        return seq

    async def _insert_input(
        self,
        conn: Connection,
        session_id: UUID,
        payload: JsonValue,
        mode: InputMode,
        event_id: UUID | None = None,
    ) -> UUID:
        input_id = uuid4()
        text = _input_text(payload)
        seq = await self._append(conn, session_id, "input", payload, text)
        await conn.execute(
            "INSERT INTO inputs(id,session_id,event_id,mode,payload,seq) VALUES "
            "(%s,%s,%s,%s,%s,%s)",
            (input_id, session_id, event_id, mode, Jsonb(payload), seq),
        )
        return input_id

    async def _deliver(self, conn: Connection, channel_id: UUID) -> dict[UUID, int]:
        events = await (
            await conn.execute(
                "SELECT * FROM events WHERE channel_id=%s AND state='pending' ORDER BY ordinal",
                (channel_id,),
            )
        ).fetchall()
        counts: dict[UUID, int] = {}
        for event in events:
            listeners = await (
                await conn.execute(
                    "SELECT s.session_id FROM subscriptions s JOIN sessions t ON t.id=s.session_id "
                    "WHERE s.channel_id=%s AND t.status <> 'deleting' "
                    "AND s.session_id IS DISTINCT FROM %s",
                    (channel_id, event["producer_session_id"]),
                )
            ).fetchall()
            if not listeners:
                continue
            envelope = cast(
                JsonValue,
                {
                    "type": "event",
                    "event_id": str(event["id"]),
                    "waiting_id": str(channel_id),
                    "producer_session_id": str(event["producer_session_id"])
                    if event["producer_session_id"]
                    else None,
                    "payload": event["payload"],
                },
            )
            for listener in listeners:
                await self._insert_input(
                    conn, listener["session_id"], envelope, event["mode"], event["id"]
                )
            await conn.execute("UPDATE events SET state='delivered' WHERE id=%s", (event["id"],))
            counts[event["id"]] = len(listeners)
        return counts

    async def _event(
        self,
        conn: Connection,
        channel_id: UUID,
        payload: JsonValue,
        producer: UUID | None,
        mode: InputMode,
    ) -> tuple[UUID, int]:
        event_id = uuid4()
        envelope: JsonValue = {
            "type": "event",
            "event_id": str(event_id),
            "waiting_id": str(channel_id),
            "producer_session_id": str(producer) if producer else None,
            "payload": payload,
        }
        # Validate before accepting backlog: every future listener must be able to store it.
        _check_record(UUID(int=0), "input", envelope, _input_text(envelope))
        await conn.execute(
            "INSERT INTO events(id,channel_id,producer_session_id,mode,payload) "
            "VALUES (%s,%s,%s,%s,%s)",
            (event_id, channel_id, producer, mode, Jsonb(payload)),
        )
        delivered = await self._deliver(conn, channel_id)
        return event_id, delivered.get(event_id, 0)

    async def create_session(
        self,
        spec: SessionSpec,
        *,
        request_id: UUID,
        input: JsonValue = None,
        mode: InputMode = "queue",
        waiting_id: UUID | None = None,
    ) -> CreatedSession:
        self._ensure_open()
        _mode(mode)
        async with self._store.write() as conn:
            prior, digest = await self._request(
                conn,
                request_id,
                "create",
                {
                    "title": spec.title,
                    "machine_ids": spec.machine_ids,
                    "default_machine_id": spec.default_machine_id,
                    "config": spec.config,
                    "input": input,
                    "mode": mode,
                    "waiting_id": waiting_id,
                },
            )
            if prior:
                saved = prior["receipt"]
                return CreatedSession(
                    _saved_view(saved["session"]), _submission(saved["submission"])
                )
            self._validate_spec(spec.title, spec.machine_ids, spec.default_machine_id, spec.config)
            bounded(spec.initial_state, CHECKPOINT_BYTES)
            bounded(input)
            session_id, channel = uuid4(), waiting_id or uuid4()
            await conn.execute(
                "INSERT INTO "
                "sessions(id,title,machine_ids,default_machine_id,config,initial_state,status) "
                "VALUES (%s,%s,%s,%s,%s,%s,'waiting')",
                (
                    session_id,
                    spec.title,
                    Jsonb(list(spec.machine_ids)),
                    spec.default_machine_id,
                    Jsonb(spec.config),
                    Jsonb(plain(spec.initial_state)),
                ),
            )
            await conn.execute("INSERT INTO subscriptions VALUES (%s,%s)", (session_id, session_id))
            input_id = (
                await self._insert_input(conn, session_id, input, mode)
                if input is not None
                else None
            )
            submission = Submission(request_id, session_id, input_id, channel)
            await conn.execute(
                "INSERT INTO "
                "requests(id,operation,fingerprint,target_session_id,input_id,waiting_id) "
                "VALUES (%s,'create',%s,%s,%s,%s)",
                (request_id, digest, session_id, input_id, channel),
            )
            if input is None:
                await self._completion(conn, session_id, None, "completed", "", [request_id])
            result = CreatedSession(_view(await self._session(conn, session_id)), submission)
            await conn.execute(
                "UPDATE requests SET receipt=%s WHERE id=%s", (Jsonb(plain(result)), request_id)
            )
        self._signal()
        return result

    def _validate_spec(
        self,
        title: str,
        machine_ids: tuple[str, ...],
        default_machine_id: str | None,
        config: JsonObject,
    ) -> None:
        if default_machine_id is not None and default_machine_id not in machine_ids:
            raise InvalidArgument("default_machine_id must be in machine_ids")
        if len(machine_ids) != len(set(machine_ids)):
            raise InvalidArgument("machine_ids must be unique")
        bounded(
            {
                "title": title,
                "machine_ids": machine_ids,
                "default_machine_id": default_machine_id,
                "config": config,
            }
        )

    async def get_session(self, session_id: UUID) -> SessionView:
        self._ensure_open()
        async with self._store.pool.connection() as conn:
            return _view(await self._session(conn, session_id))

    async def list_sessions(
        self,
        *,
        session_ids: tuple[UUID, ...] | None = None,
        after: UUID | None = None,
        limit: int = 100,
    ) -> SessionPage:
        self._ensure_open()
        page_limit(limit)
        async with self._store.pool.connection() as conn, conn.transaction():
            async with conn.cursor(name="sessions_" + uuid4().hex) as cur:
                await cur.execute(
                    "SELECT "
                    + _SESSION_COLUMNS
                    + " FROM sessions WHERE (%s::uuid[] IS NULL OR id=ANY(%s)) "
                    "AND (%s::uuid IS NULL OR id>%s) ORDER BY id LIMIT %s",
                    (
                        list(session_ids) if session_ids is not None else None,
                        list(session_ids) if session_ids is not None else None,
                        after,
                        after,
                        limit + 1,
                    ),
                )
                items: list[SessionView] = []
                while row := await cur.fetchone():
                    item = _view(row)
                    if (
                        len(items) == limit
                        or len(encode(SessionPage(tuple([*items, item]), item.id))) > PAGE_BYTES
                    ):
                        if not items:
                            raise QueryLimitExceeded("session does not fit a page")
                        return SessionPage(tuple(items), items[-1].id)
                    items.append(item)
                return SessionPage(tuple(items), None)

    async def update_session(
        self,
        session_id: UUID,
        *,
        request_id: UUID,
        title: str,
        machine_ids: tuple[str, ...],
        default_machine_id: str | None,
        config: JsonObject,
    ) -> SessionView:
        self._ensure_open()
        async with self._store.write() as conn:
            prior, digest = await self._request(
                conn,
                request_id,
                "update",
                {
                    "session_id": session_id,
                    "title": title,
                    "machine_ids": machine_ids,
                    "default_machine_id": default_machine_id,
                    "config": config,
                },
            )
            if prior:
                return _saved_view(prior["receipt"])
            self._validate_spec(title, machine_ids, default_machine_id, config)
            row = await self._session(conn, session_id)
            if row["status"] != "waiting":
                raise Conflict("session settings can only change while waiting")
            row = await (
                await conn.execute(
                    "UPDATE sessions SET title=%s,machine_ids=%s,default_machine_id=%s,config=%s,"
                    "updated_at=now() WHERE id=%s RETURNING " + _SESSION_COLUMNS,
                    (
                        title,
                        Jsonb(list(machine_ids)),
                        default_machine_id,
                        Jsonb(config),
                        session_id,
                    ),
                )
            ).fetchone()
            assert row is not None
            result = _view(row)
            await conn.execute(
                "INSERT INTO requests(id,operation,fingerprint,receipt,target_session_id) "
                "VALUES (%s,'update',%s,%s,%s)",
                (request_id, digest, Jsonb(plain(result)), session_id),
            )
        self._signal()
        return result

    async def delete_session(self, session_id: UUID, *, request_id: UUID) -> bool:
        self._ensure_open()
        async with self._store.write() as conn:
            prior, digest = await self._request(
                conn, request_id, "delete", {"session_id": session_id}
            )
            if prior and prior["receipt"] is not None:
                return cast(bool, prior["receipt"])
            row = await (
                await conn.execute(
                    "UPDATE sessions SET status='deleting' WHERE id=%s RETURNING id",
                    (session_id,),
                )
            ).fetchone()
            if not prior:
                await conn.execute(
                    "INSERT INTO requests(id,operation,fingerprint,receipt,target_session_id) "
                    "VALUES (%s,'delete',%s,%s,%s)",
                    (request_id, digest, Jsonb(False) if not row else None, session_id),
                )
            if not row:
                return False
        task = self._tasks.get(session_id)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._finish_delete(session_id)
        self._signal()
        return True

    async def _finish_delete(self, session_id: UUID) -> None:
        async with self._store.write() as conn:
            row = await (
                await conn.execute(
                    "SELECT id,status,latest_run_id FROM sessions WHERE id=%s",
                    (session_id,),
                )
            ).fetchone()
            if not row:
                return
            if row["status"] != "deleting":
                raise Conflict("session is not deleting")
            requests = await (
                await conn.execute(
                    "SELECT id FROM requests WHERE target_session_id=%s AND completion IS NULL "
                    "AND operation IN ('create','input') ORDER BY id",
                    (session_id,),
                )
            ).fetchall()
            await self._completion(
                conn,
                session_id,
                row["latest_run_id"],
                "deleted",
                "",
                [request["id"] for request in requests],
            )
            await conn.execute("DELETE FROM sessions WHERE id=%s", (session_id,))
            await conn.execute(
                "UPDATE requests SET receipt=%s WHERE target_session_id=%s "
                "AND operation='delete' AND receipt IS NULL",
                (Jsonb(True), session_id),
            )

    async def submit_input(
        self,
        session_id: UUID,
        payload: JsonValue,
        *,
        request_id: UUID,
        mode: InputMode = "steer",
        waiting_id: UUID | None = None,
    ) -> Submission:
        self._ensure_open()
        _mode(mode)
        bounded(payload)
        async with self._store.write() as conn:
            prior, digest = await self._request(
                conn,
                request_id,
                "input",
                {
                    "session_id": session_id,
                    "payload": payload,
                    "mode": mode,
                    "waiting_id": waiting_id,
                },
            )
            if prior:
                return _submission(prior["receipt"])
            session = await self._session(conn, session_id)
            if session["status"] == "deleting":
                raise Conflict("session is deleting")
            channel = waiting_id or uuid4()
            input_id = await self._insert_input(conn, session_id, payload, mode)
            result = Submission(request_id, session_id, input_id, channel)
            await conn.execute(
                "INSERT INTO requests(id,operation,fingerprint,receipt,target_session_id,input_id,"
                "waiting_id) VALUES (%s,'input',%s,%s,%s,%s,%s)",
                (request_id, digest, Jsonb(plain(result)), session_id, input_id, channel),
            )
        self._signal()
        return result

    async def publish_event(
        self,
        waiting_id: UUID,
        payload: JsonValue,
        *,
        request_id: UUID,
        producer_session_id: UUID | None,
        mode: InputMode = "steer",
    ) -> EventReceipt:
        self._ensure_open()
        _mode(mode)
        bounded(payload)
        async with self._store.write() as conn:
            prior, digest = await self._request(
                conn,
                request_id,
                "publish",
                {
                    "waiting_id": waiting_id,
                    "payload": payload,
                    "producer_session_id": producer_session_id,
                    "mode": mode,
                },
            )
            if prior:
                saved = prior["receipt"]
                return EventReceipt(
                    request_id,
                    UUID(saved["event_id"]),
                    UUID(saved["waiting_id"]),
                    saved["delivered"],
                    saved["pending"],
                )
            event_id, delivered = await self._event(
                conn, waiting_id, payload, producer_session_id, mode
            )
            result = EventReceipt(request_id, event_id, waiting_id, delivered, not delivered)
            await conn.execute(
                "INSERT INTO requests(id,operation,fingerprint,receipt) VALUES "
                "(%s,'publish',%s,%s)",
                (request_id, digest, Jsonb(plain(result))),
            )
        self._signal()
        return result

    async def _completion(
        self,
        conn: Connection,
        session_id: UUID,
        run_id: UUID | None,
        outcome: Literal["completed", "failed", "deleted"],
        output: str,
        request_ids: list[UUID],
    ) -> None:
        completed_at = datetime.now(UTC)
        # A run can consume arbitrarily many requests through repeated steer polls. Keep every
        # receipt, but never aggregate its entire request set into one unbounded channel payload.
        for start in range(0, max(1, len(request_ids)), 64):
            batch = request_ids[start : start + 64]
            session = await self._session(conn, session_id)
            position = cursor(session_id, session["next_seq"] + 1)
            payload = cast(
                JsonObject,
                {
                    "type": "session.waiting",
                    "session_id": str(session_id),
                    "run_id": str(run_id) if run_id else None,
                    "request_ids": [str(value) for value in batch],
                    "outcome": outcome,
                    "output": output,
                    "cursor": position,
                },
            )
            await self._append(conn, session_id, "waiting", payload, run_id=run_id)
            await self._event(conn, session_id, payload, session_id, "steer")
            for request_id in batch:
                request = await (
                    await conn.execute(
                        "SELECT waiting_id FROM requests WHERE id=%s AND completion IS NULL",
                        (request_id,),
                    )
                ).fetchone()
                if not request:
                    continue
                completion = plain(
                    {
                        "run_id": run_id,
                        "outcome": outcome,
                        "output": output,
                        "cursor": position,
                        "completed_at": completed_at,
                    }
                )
                await conn.execute(
                    "UPDATE requests SET completed_run_id=%s,completion=%s WHERE id=%s",
                    (run_id, Jsonb(completion), request_id),
                )
                if request["waiting_id"] != session_id:
                    await self._event(
                        conn,
                        request["waiting_id"],
                        {**payload, "request_ids": [str(request_id)]},
                        session_id,
                        "steer",
                    )

    async def _prepare(self, session_id: UUID) -> _Context | None:
        async with self._store.write() as conn:
            row = await (
                await conn.execute("SELECT * FROM sessions WHERE id=%s", (session_id,))
            ).fetchone()
            if not row or row["status"] == "deleting":
                return None
            recovered = row["status"] == "running"
            if recovered:
                run = await (
                    await conn.execute("SELECT * FROM runs WHERE id=%s", (row["latest_run_id"],))
                ).fetchone()
                assert run is not None
                await self._append(
                    conn,
                    session_id,
                    "interrupted",
                    {"reason": "resuming interrupted attempt"},
                    run_id=run["id"],
                    attempt=run["attempt"],
                )
                run = await (
                    await conn.execute(
                        "UPDATE runs SET attempt=attempt+1,status='running' WHERE id=%s "
                        "RETURNING *",
                        (run["id"],),
                    )
                ).fetchone()
                assert run is not None
                inputs = await (
                    await conn.execute(
                        "SELECT * FROM inputs WHERE run_id=%s AND state='reserved' ORDER BY seq",
                        (run["id"],),
                    )
                ).fetchall()
            else:
                inputs = await (
                    await conn.execute(
                        "SELECT * FROM inputs WHERE session_id=%s AND state='pending' ORDER BY "
                        "seq LIMIT 64",
                        (session_id,),
                    )
                ).fetchall()
                if not inputs:
                    return None
                previous = (
                    await (
                        await conn.execute(
                            "SELECT runner_state FROM runs WHERE id=%s",
                            (row["latest_run_id"],),
                        )
                    ).fetchone()
                    if row["latest_run_id"]
                    else None
                )
                state = previous["runner_state"] if previous else row["initial_state"]
                run_id = uuid4()
                run = await (
                    await conn.execute(
                        "INSERT INTO runs(id,session_id,status,runner_state) VALUES "
                        "(%s,%s,'running',%s) "
                        "RETURNING *",
                        (run_id, session_id, Jsonb(state)),
                    )
                ).fetchone()
                assert run is not None
                await conn.execute(
                    "UPDATE inputs SET state='reserved',run_id=%s WHERE id=ANY(%s)",
                    (run_id, [value["id"] for value in inputs]),
                )
                await conn.execute(
                    "UPDATE sessions SET "
                    "status='running',latest_run_id=%s,updated_at=now() WHERE id=%s",
                    (run_id, session_id),
                )
            session = _view(await self._session(conn, session_id))
            return _Context(
                self,
                session,
                run["id"],
                run["attempt"],
                recovered,
                tuple(_input(item) for item in inputs),
                _state(run["runner_state"]),
                run["checkpoint_no"],
            )

    async def _attempt(self, conn: Connection, context: _Context) -> dict[str, Any]:
        row = await (
            await conn.execute(
                "SELECT r.* FROM runs r JOIN sessions s ON s.id=r.session_id "
                "WHERE r.id=%s AND r.attempt=%s AND r.status='running' AND s.status='running' "
                "AND s.latest_run_id=r.id",
                (context.run_id, context.attempt),
            )
        ).fetchone()
        if not row:
            raise Conflict("run attempt is no longer active")
        return row

    async def _poll(self, context: _Context, limit: int) -> tuple[SessionInput, ...]:
        if isinstance(limit, bool) or not 1 <= limit <= 64:
            raise InvalidArgument("steer limit must be between 1 and 64")
        async with self._store.write() as conn:
            await self._attempt(conn, context)
            rows = await (
                await conn.execute(
                    "SELECT * FROM inputs WHERE session_id=%s AND state='pending' AND mode='steer' "
                    "ORDER BY seq LIMIT %s",
                    (context.session.id, limit),
                )
            ).fetchall()
            if rows:
                await conn.execute(
                    "UPDATE inputs SET state='reserved',run_id=%s WHERE id=ANY(%s)",
                    (context.run_id, [row["id"] for row in rows]),
                )
        return tuple(_input(row) for row in rows)

    async def _emit(self, context: _Context, delta: OutputDelta) -> Cursor:
        bounded(delta, DELTA_BYTES)
        if delta.kind not in ("text_delta", "tool_call", "tool_result", "notice"):
            raise InvalidArgument("invalid output delta kind")
        digest = fingerprint(delta)
        async with self._store.write() as conn:
            await self._attempt(conn, context)
            prior = await (
                await conn.execute(
                    "SELECT * FROM records WHERE session_id=%s AND emission_id=%s",
                    (context.session.id, delta.emission_id),
                )
            ).fetchone()
            if prior:
                if prior["emission_fingerprint"] != digest or prior["run_id"] != context.run_id:
                    raise Conflict("emission_id was used with different output")
                return cursor(context.session.id, prior["seq"])
            seq = await self._append(
                conn,
                context.session.id,
                delta.kind,
                delta.data,
                delta.data if isinstance(delta.data, str) else "",
                run_id=context.run_id,
                attempt=context.attempt,
                message_id=delta.message_id,
                emission_id=delta.emission_id,
                emission_fingerprint=digest,
            )
        self._signal()
        return cursor(context.session.id, seq)

    async def _checkpoint(
        self,
        conn: Connection,
        context: _Context,
        write: CheckpointWrite,
        *,
        final: bool = False,
    ) -> int:
        bounded(write, CHECKPOINT_BYTES)
        run = await self._attempt(conn, context)
        digest = fingerprint(write)
        previous = await (
            await conn.execute(
                "SELECT * FROM checkpoints WHERE run_id=%s AND number=%s",
                (context.run_id, write.number),
            )
        ).fetchone()
        if previous and not final:
            if previous["fingerprint"] != digest:
                raise Conflict("checkpoint number was used with different content")
            return previous["cursor_seq"]
        if isinstance(write.number, bool) or write.number != run["checkpoint_no"] + 1:
            raise Conflict("checkpoint must use the next number")
        ids = list(write.consumed_input_ids)
        if len(ids) != len(set(ids)):
            raise InvalidArgument("checkpoint input ids must be unique")
        if ids:
            inputs = await (
                await conn.execute(
                    "SELECT id FROM inputs WHERE run_id=%s AND state='reserved' AND id=ANY(%s)",
                    (context.run_id, ids),
                )
            ).fetchall()
            if len(inputs) != len(ids):
                raise Conflict("checkpoint can only consume this run's reserved inputs")
            await conn.execute("UPDATE inputs SET state='consumed' WHERE id=ANY(%s)", (ids,))
        for message in write.messages:
            bounded(message)
            if message.kind not in ("model_request", "model_response"):
                raise InvalidArgument("invalid complete message kind")
            await self._append(
                conn,
                context.session.id,
                message.kind,
                message.data,
                message.text,
                run_id=context.run_id,
                attempt=context.attempt,
                message_id=message.message_id,
            )
        await conn.execute(
            "UPDATE runs SET checkpoint_no=%s,runner_state=%s WHERE id=%s",
            (write.number, Jsonb(plain(write.state)), context.run_id),
        )
        seq = (await self._session(conn, context.session.id))["next_seq"]
        await conn.execute(
            "INSERT INTO checkpoints VALUES (%s,%s,%s,%s)",
            (context.run_id, write.number, digest, seq),
        )
        return seq

    async def _save_checkpoint(self, context: _Context, write: CheckpointWrite) -> Cursor:
        async with self._store.write() as conn:
            seq = await self._checkpoint(conn, context, write)
        self._signal()
        return cursor(context.session.id, seq)

    async def _finish(self, context: _Context, result: RunResult) -> None:
        bounded(result.output)
        if len(result.wait_for) > 128 or not all(
            isinstance(item, UUID) for item in result.wait_for
        ):
            raise InvalidArgument("wait_for must contain at most 128 UUIDs")
        async with self._store.write() as conn:
            await self._checkpoint(conn, context, result.checkpoint, final=True)
            reserved = await (
                await conn.execute(
                    "SELECT 1 FROM inputs WHERE run_id=%s AND state='reserved' LIMIT 1",
                    (context.run_id,),
                )
            ).fetchone()
            if reserved:
                raise Conflict("runner returned with unconfirmed inputs")
            await self._append(
                conn,
                context.session.id,
                "final",
                {"output": result.output},
                result.output,
                run_id=context.run_id,
                attempt=context.attempt,
            )
            await conn.execute(
                "UPDATE runs SET status='waiting',finished_at=now() WHERE id=%s", (context.run_id,)
            )
            await conn.execute(
                "UPDATE sessions SET status='waiting' WHERE id=%s", (context.session.id,)
            )
            channels = list(set(result.wait_for) | {context.session.id})
            await conn.execute(
                "DELETE FROM subscriptions WHERE session_id=%s AND NOT(channel_id=ANY(%s))",
                (context.session.id, channels),
            )
            for channel in channels:
                await conn.execute(
                    "INSERT INTO subscriptions VALUES (%s,%s) ON CONFLICT DO NOTHING",
                    (channel, context.session.id),
                )
                await self._deliver(conn, channel)
            requests = await (
                await conn.execute(
                    "SELECT r.id FROM requests r JOIN inputs i ON i.id=r.input_id "
                    "WHERE i.run_id=%s AND i.state='consumed' AND r.completion IS NULL "
                    "ORDER BY i.seq",
                    (context.run_id,),
                )
            ).fetchall()
            await self._completion(
                conn,
                context.session.id,
                context.run_id,
                "completed",
                result.output,
                [request["id"] for request in requests],
            )
        self._signal()

    async def _fail(self, context: _Context, error: Exception) -> None:
        self._ensure_open()
        async with self._store.write() as conn:
            await self._attempt(conn, context)
            # Only an explicitly trusted RunFailure can expose a public explanation.
            # Other exception strings may contain provider secrets or prompts.
            public_message = error.public_message if isinstance(error, RunFailure) else ""
            detail: JsonObject = (
                {"kind": error.code, "public_message": public_message}
                if isinstance(error, RunFailure)
                else {"kind": type(error).__name__, "message": "runner failed"}
            )
            await self._append(
                conn,
                context.session.id,
                "error",
                detail,
                public_message or "runner failed",
                run_id=context.run_id,
                attempt=context.attempt,
            )
            await conn.execute(
                "UPDATE inputs SET state='consumed' WHERE run_id=%s AND state='reserved'",
                (context.run_id,),
            )
            await conn.execute(
                "UPDATE runs SET status='failed',finished_at=now() WHERE id=%s", (context.run_id,)
            )
            await conn.execute(
                "UPDATE sessions SET status='waiting' WHERE id=%s", (context.session.id,)
            )
            requests = await (
                await conn.execute(
                    "SELECT r.id FROM requests r JOIN inputs i ON i.id=r.input_id "
                    "WHERE i.run_id=%s AND r.completion IS NULL ORDER BY i.seq",
                    (context.run_id,),
                )
            ).fetchall()
            await self._completion(
                conn,
                context.session.id,
                context.run_id,
                "failed",
                public_message,
                [request["id"] for request in requests],
            )
        self._signal()

    async def _run_session(self, session_id: UUID) -> None:
        try:
            session = await self.get_session(session_id)
            if session.status == "deleting":
                await self._finish_delete(session_id)
                return
            context = await self._prepare(session_id)
        except NotFound, ServiceUnavailable:
            return
        if context is None:
            return
        try:
            result = await self._runner(context)
            await self._finish(context, result)
        except asyncio.CancelledError:
            # Leaving status=running is intentional: the next owner reuses run_id and checkpoint.
            raise
        except Exception as exc:
            # Runner exceptions use the failure path even if they happen to be StateError types.
            # Only the durable attempt/lease checks below decide whether completion is still valid.
            try:
                await self._fail(context, exc)
            except Conflict, NotFound, ServiceUnavailable:
                pass

    async def _read_page(
        self,
        session_id: UUID,
        after: Cursor | None,
        limit: int,
        *,
        history: bool = False,
        search: str | None = None,
        mode: Literal["substring", "fulltext"] = "fulltext",
        snapshot: Cursor | None = None,
        export: bool = False,
    ) -> RecordPage | HistoryExportPage:
        self._ensure_open()
        page_limit(limit)
        seq = sequence(session_id, after)
        async with self._store.pool.connection() as conn, conn.transaction():
            session = await self._session(conn, session_id)
            upper = session["next_seq"] if snapshot is None else sequence(session_id, snapshot)
            if seq > upper or upper > session["next_seq"]:
                raise InvalidArgument("cursor is outside this session snapshot")
            snapshot_cursor = cursor(session_id, upper)

            def page(
                items: tuple[Record, ...],
                position: Cursor,
                has_more: bool,
            ) -> RecordPage | HistoryExportPage:
                if export:
                    return HistoryExportPage(items, position, snapshot_cursor, has_more)
                return RecordPage(items, position, has_more)

            conditions: list[LiteralString] = ["session_id=%s", "seq>%s", "seq<=%s"]
            values: list[Any] = [session_id, seq, upper]
            if history:
                conditions.append("kind=ANY(%s)")
                values.append(list(HISTORY_KINDS))
            if search is not None:
                if mode == "substring":
                    if len(search.encode()) > 16 * 1024:
                        raise InvalidArgument("search query exceeds 16 KiB")
                    conditions.append("strpos(text,%s)>0")
                    values.append(search)
                elif mode == "fulltext":
                    terms, verify = search_terms(search)
                    if not terms:
                        return page((), snapshot_cursor, False)
                    conditions.append("search_vector @@ plainto_tsquery('pg_catalog.simple',%s)")
                    values.append(terms)
                    for word in verify:
                        conditions.append("strpos(normalized_text,%s)>0")
                        values.append(word)
                else:
                    raise InvalidArgument("unknown history search mode")
            values.append(limit + 1)
            items: list[Record] = []
            position = cursor(session_id, seq)
            has_more = False
            async with conn.cursor(name="records_" + uuid4().hex) as cur:
                await cur.execute(
                    psycopg.sql.SQL(
                        "SELECT " + _RECORD_COLUMNS + " FROM records WHERE {} ORDER BY seq LIMIT %s"
                    ).format(psycopg.sql.SQL(" AND ").join(psycopg.sql.SQL(c) for c in conditions)),
                    values,
                )
                while row := await cur.fetchone():
                    item = _record_view(row)
                    if (
                        len(items) == limit
                        or len(encode(page(tuple([*items, item]), snapshot_cursor, False)))
                        > PAGE_BYTES
                    ):
                        if not items:
                            raise QueryLimitExceeded("record does not fit a page")
                        has_more = True
                        break
                    items.append(item)
                    position = item.cursor
            if not has_more:
                # Only advance through the bound snapshot, never across concurrent new records.
                position = snapshot_cursor
            return page(tuple(items), position, has_more)

    async def read_output(
        self,
        session_id: UUID,
        *,
        after: Cursor | None = None,
        limit: int = 200,
        wait_seconds: float = 0,
    ) -> RecordPage:
        _wait(wait_seconds)
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            changed = self._changed
            page = cast(RecordPage, await self._read_page(session_id, after, limit))
            remaining = deadline - asyncio.get_running_loop().time()
            if page.items or remaining <= 0:
                return page
            await self._pause(changed, min(1, remaining))

    async def read_history(
        self,
        session_id: UUID,
        *,
        after: Cursor | None = None,
        limit: int = 200,
    ) -> RecordPage:
        return cast(RecordPage, await self._read_page(session_id, after, limit, history=True))

    async def search_history(
        self,
        session_id: UUID,
        query: str,
        *,
        mode: Literal["substring", "fulltext"] = "fulltext",
        after: Cursor | None = None,
        limit: int = 100,
    ) -> RecordPage:
        return cast(
            RecordPage,
            await self._read_page(session_id, after, limit, history=True, search=query, mode=mode),
        )

    async def export_history(
        self,
        session_id: UUID,
        *,
        after: Cursor | None = None,
        snapshot: Cursor | None = None,
        limit: int = 200,
    ) -> HistoryExportPage:
        return cast(
            HistoryExportPage,
            await self._read_page(
                session_id,
                after,
                limit,
                history=True,
                snapshot=snapshot,
                export=True,
            ),
        )

    async def wait_submission(
        self,
        session_id: UUID,
        request_id: UUID,
        *,
        wait_seconds: float = 0,
    ) -> SubmissionStatus:
        _wait(wait_seconds)
        deadline = asyncio.get_running_loop().time() + wait_seconds
        while True:
            self._ensure_open()
            changed = self._changed
            async with self._store.pool.connection() as conn:
                row = await (
                    await conn.execute(
                        "SELECT operation,receipt,completion FROM requests WHERE id=%s "
                        "AND target_session_id=%s AND operation IN ('create','input')",
                        (request_id, session_id),
                    )
                ).fetchone()
            if not row:
                raise NotFound("submission does not exist in this session")
            receipt = row["receipt"]
            submission = _submission(
                receipt["submission"] if row["operation"] == "create" else receipt
            )
            saved = row["completion"]
            completion = (
                None
                if saved is None
                else Completion(
                    UUID(saved["run_id"]) if saved["run_id"] else None,
                    saved["outcome"],
                    saved["output"],
                    saved["cursor"],
                    datetime.fromisoformat(saved["completed_at"]),
                )
            )
            remaining = deadline - asyncio.get_running_loop().time()
            if completion is not None or remaining <= 0:
                return SubmissionStatus(submission, completion)
            await self._pause(changed, min(1, remaining))

    async def query_history(
        self,
        session_id: UUID,
        sql: str,
        *,
        params: JsonObject | None = None,
        limit: int = 200,
    ) -> QueryResult:
        self._ensure_open()
        page_limit(limit)
        statement, values = compile_query(sql, self._store.schema, session_id, params)
        try:
            async with self._store.pool.connection() as conn, conn.transaction():
                await conn.execute("SET TRANSACTION READ ONLY")
                await conn.execute("SET LOCAL search_path TO pg_catalog")
                await conn.execute("SET LOCAL statement_timeout TO '2s'")
                await conn.execute("SET LOCAL lock_timeout TO '250ms'")
                # The API check cannot use the connection's temporarily restricted search_path.
                exists = await (
                    await conn.execute(
                        psycopg.sql.SQL("SELECT 1 FROM {}.sessions WHERE id=%s").format(
                            psycopg.sql.Identifier(self._store.schema)
                        ),
                        (session_id,),
                    )
                ).fetchone()
                if not exists:
                    raise NotFound("session does not exist")
                async with conn.cursor(name="history_" + uuid4().hex, row_factory=tuple_row) as cur:
                    await cur.execute(statement, values)
                    columns = tuple(column.name for column in cur.description or ())
                    rows: list[tuple[JsonValue, ...]] = []
                    while row := await cur.fetchone():
                        if len(rows) == limit:
                            return QueryResult(columns, tuple(rows), True)
                        rows.append(tuple(plain(value) for value in row))
                        if len(encode(QueryResult(columns, tuple(rows), False))) > PAGE_BYTES:
                            raise QueryLimitExceeded("query result exceeds 512 KiB")
                    return QueryResult(columns, tuple(rows), False)
        except (psycopg.errors.QueryCanceled, psycopg.errors.LockNotAvailable) as exc:
            raise QueryLimitExceeded("query execution limit exceeded") from exc
        except psycopg.Error as exc:
            raise InvalidArgument("query is invalid for the history columns") from exc


class _Context:
    def __init__(
        self,
        service: SessionService,
        session: SessionView,
        run_id: UUID,
        attempt: int,
        recovered: bool,
        inputs: tuple[SessionInput, ...],
        state: RunnerState,
        checkpoint_number: int,
    ) -> None:
        self._service = service
        self.session = session
        self.run_id = run_id
        self.attempt = attempt
        self.recovered = recovered
        self.inputs = inputs
        self.state = state
        self.checkpoint_number = checkpoint_number

    async def poll_steer(self, *, limit: int = 64) -> tuple[SessionInput, ...]:
        return await self._service._poll(self, limit)

    async def emit(self, delta: OutputDelta) -> Cursor:
        return await self._service._emit(self, delta)

    async def checkpoint(self, write: CheckpointWrite) -> Cursor:
        result = await self._service._save_checkpoint(self, write)
        if write.number > self.checkpoint_number:
            self.checkpoint_number = write.number
            self.state = write.state
        return result

    async def read_history(self, *, after: Cursor | None = None, limit: int = 200) -> RecordPage:
        return await self._service.read_history(self.session.id, after=after, limit=limit)

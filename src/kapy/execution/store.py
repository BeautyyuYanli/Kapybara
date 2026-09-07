"""Exclusive XDG state, SQLite transactions and persistent session identities."""

import asyncio
import fcntl
import hmac
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from kapy.rpc import JsonObject

from ._common import IOWorker, error, session_identifier, string
from .paths import ExecutionPaths


def secure_directory(path: Path) -> None:
    missing: list[Path] = []
    candidate = path
    while not candidate.exists():
        missing.append(candidate)
        candidate = candidate.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("Execution directories must be owned by this UID with mode 0700")


def _private_file(path: Path) -> int:
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise RuntimeError("Execution state files must be private regular files owned by this UID")
    return fd


@dataclass(slots=True)
class TransferRecord:
    session_id: str
    transfer_id: str
    fingerprint: str
    path: str
    staging_path: str | None
    info: JsonObject


class ExecutionStore:
    """One daemon's locked SQLite connection and in-memory session credentials."""

    def __init__(self, paths: ExecutionPaths, machine_id: str) -> None:
        self.paths = paths
        self.machine_id = string(machine_id, "machine_id")
        self.io = IOWorker()
        self._lock = asyncio.Lock()
        self._connection: sqlite3.Connection | None = None
        self._lock_fds: list[int] = []
        self._tokens: dict[str, str] = {}
        self._entered = False

    async def __aenter__(self) -> Self:
        if self._entered:
            raise RuntimeError("ExecutionStore is single-use")
        self._entered = True
        try:
            await self.io.run(self._open)
        except BaseException:
            await self.aclose()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _open(self) -> None:
        try:
            for root in (self.paths.state_dir, self.paths.data_dir, self.paths.runtime_dir):
                secure_directory(root)
            for path in dict.fromkeys(
                (self.paths.state_dir / "daemon.lock", self.paths.runtime_dir / "daemon.lock")
            ):
                fd = _private_file(path)
                self._lock_fds.append(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError(
                        "Another daemon owns this execution state or runtime"
                    ) from exc
            db_path = self.paths.state_dir / "execution.sqlite3"
            os.close(_private_file(db_path))
            connection = sqlite3.connect(db_path, check_same_thread=False, timeout=5)
            self._connection = connection
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA busy_timeout=5000")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError("Unsupported execution database version")
            with connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS daemon_meta (
                        singleton INTEGER PRIMARY KEY CHECK(singleton = 1), machine_id TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS sessions (
                        session_id TEXT PRIMARY KEY, cwd TEXT NOT NULL,
                        state TEXT NOT NULL CHECK(state IN ('active','releasing','released')),
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    CREATE TABLE IF NOT EXISTS transfers (
                        session_id TEXT NOT NULL REFERENCES sessions(session_id),
                        transfer_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                        path TEXT NOT NULL, staging_path TEXT,
                        state TEXT NOT NULL, info_json TEXT NOT NULL,
                        PRIMARY KEY(session_id, transfer_id)
                    );
                    CREATE INDEX IF NOT EXISTS transfers_active ON transfers(state);
                    PRAGMA user_version=1;
                    """
                )
                connection.execute(
                    "INSERT OR IGNORE INTO daemon_meta(singleton,machine_id) VALUES(1,?)",
                    (self.machine_id,),
                )
                actual = connection.execute("SELECT machine_id FROM daemon_meta").fetchone()[0]
                if actual != self.machine_id:
                    raise RuntimeError("Execution state belongs to a different machine_id")
        except BaseException:
            self._close()
            raise

    def _close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        for fd in self._lock_fds:
            os.close(fd)
        self._lock_fds.clear()

    async def aclose(self) -> None:
        async with self._lock:
            self._tokens.clear()
            await self.io.run(self._close)

    async def transaction[T](self, operation: Callable[[sqlite3.Connection], T]) -> T:
        async with self._lock:
            connection = self._connection
            if connection is None:
                raise RuntimeError("ExecutionStore is closed")

            def perform() -> T:
                with connection:
                    return operation(connection)

            return await self.io.run(perform)

    async def ensure_session(self, session_id: str, token: str) -> JsonObject:
        session_id = session_identifier(session_id)
        token = string(token, "session_token")
        cwd = self.paths.session_cwd(session_id)

        def ensure(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT state FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is not None and row[0] != "active":
                raise error(
                    "gone" if row[0] == "released" else "conflict", "Session is being released"
                )
            secure_directory(cwd)
            connection.execute(
                "INSERT OR IGNORE INTO sessions(session_id,cwd,state) VALUES(?,?,'active')",
                (session_id, str(cwd)),
            )

        await self.transaction(ensure)
        self._tokens[session_id] = token
        return {"session_id": session_id, "cwd": str(cwd)}

    async def session_cwd(self, session_id: str, *, require_token: bool = False) -> Path:
        session_id = session_identifier(session_id)

        def find(connection: sqlite3.Connection) -> Path:
            row = connection.execute(
                "SELECT cwd,state FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise error("not_found", "Session not found")
            if row["state"] != "active":
                raise error(
                    "gone" if row["state"] == "released" else "conflict",
                    "Session is being released",
                )
            return Path(row["cwd"])

        cwd = await self.transaction(find)
        if require_token and session_id not in self._tokens:
            raise error("unauthorized", "Session must be ensured on this daemon connection")
        return cwd

    def session_token(self, session_id: str) -> str:
        try:
            return self._tokens[session_id]
        except KeyError as exc:
            raise error("unauthorized", "Session credentials are unavailable") from exc

    def authenticate_session(self, session_id: str, token: str) -> bool:
        expected = self._tokens.get(session_id)
        return expected is not None and hmac.compare_digest(expected.encode(), token.encode())

    async def begin_release(self, session_id: str) -> bool:
        session_id = session_identifier(session_id)
        self._tokens.pop(session_id, None)

        def begin(connection: sqlite3.Connection) -> bool:
            row = connection.execute(
                "SELECT state FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None or row[0] == "released":
                return False
            connection.execute(
                "UPDATE sessions SET state='releasing' WHERE session_id=?", (session_id,)
            )
            return True

        result = await self.transaction(begin)
        self._tokens.pop(session_id, None)
        return result

    async def finish_release(self, session_id: str) -> None:
        session_root = self.paths.session_cwd(session_identifier(session_id)).parent

        def finish(connection: sqlite3.Connection) -> None:
            row = connection.execute(
                "SELECT state FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None or row[0] == "released":
                return
            if row[0] != "releasing":
                raise error("conflict", "Session cleanup has not started")
            if session_root.exists():
                shutil.rmtree(session_root)
            connection.execute(
                "UPDATE sessions SET state='released' WHERE session_id=?", (session_id,)
            )

        await self.transaction(finish)

    @staticmethod
    def _transfer(row: sqlite3.Row) -> TransferRecord:
        return TransferRecord(
            row["session_id"],
            row["transfer_id"],
            row["fingerprint"],
            row["path"],
            row["staging_path"],
            json.loads(row["info_json"]),
        )

    async def get_transfer(self, session_id: str, transfer_id: str) -> TransferRecord | None:
        def get(connection: sqlite3.Connection) -> TransferRecord | None:
            row = connection.execute(
                "SELECT * FROM transfers WHERE session_id=? AND transfer_id=?",
                (session_id, transfer_id),
            ).fetchone()
            return None if row is None else self._transfer(row)

        return await self.transaction(get)

    async def save_transfer(self, record: TransferRecord) -> None:
        def save(connection: sqlite3.Connection) -> None:
            connection.execute(
                """INSERT INTO transfers
                   (session_id,transfer_id,fingerprint,path,staging_path,state,info_json)
                   VALUES(?,?,?,?,?,?,?) ON CONFLICT(session_id,transfer_id) DO UPDATE SET
                   staging_path=excluded.staging_path,state=excluded.state,info_json=excluded.info_json""",
                (
                    record.session_id,
                    record.transfer_id,
                    record.fingerprint,
                    record.path,
                    record.staging_path,
                    record.info["state"],
                    json.dumps(record.info, separators=(",", ":")),
                ),
            )

        await self.transaction(save)

    async def unfinished_transfers(self) -> list[TransferRecord]:
        def get(connection: sqlite3.Connection) -> list[TransferRecord]:
            rows = connection.execute(
                "SELECT * FROM transfers WHERE state IN ('open','running') LIMIT 64"
            ).fetchall()
            return [self._transfer(row) for row in rows]

        return await self.transaction(get)

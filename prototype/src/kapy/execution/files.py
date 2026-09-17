"""Bounded machine file transfers with durable IDs and atomic push commits."""

import asyncio
import hashlib
import ipaddress
import os
import stat
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import httpx2

from kapy.rpc import JsonObject, JsonParams, JsonValue, RpcError

from ._common import (
    CHUNK_SIZE,
    byte_chunk,
    decode_chunk,
    error,
    fields,
    fingerprint,
    identifier,
    integer,
    invalid,
    session_identifier,
    string,
)
from .store import ExecutionStore, TransferRecord

MAX_TRANSFERS = 8
TRANSFER_IDLE_SECONDS = 600.0


@dataclass(slots=True)
class _Transfer:
    record: TransferRecord
    kind: str
    fd: int = -1
    parent_fd: int = -1
    signature: tuple[int, int, int, int, int] | None = None
    destination: tuple[int, int, str] | None = None
    digest: Any = None
    last_offset: int = -1
    last_size: int = 0
    last_activity: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    done: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _transport(value: JsonValue) -> JsonObject:
    if not isinstance(value, dict):
        raise invalid("transport must be an object")
    if value.get("kind") == "websocket":
        fields(value, {"kind"})
        return {"kind": "websocket"}
    fields(value, {"kind", "url"}, {"headers"})
    if value["kind"] != "url":
        raise invalid("Unknown transfer transport")
    url = string(value["url"], "url")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        _ = parsed.port  # urlsplit validates the port lazily.
        loopback = hostname == "localhost"
        if hostname is not None and not loopback:
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                pass
        if (
            not hostname
            or (parsed.scheme != "https" and not (parsed.scheme == "http" and loopback))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError
    except ValueError as exc:
        raise invalid("URL must use HTTPS, except for loopback HTTP tests") from exc
    source = value.get("headers", {})
    if not isinstance(source, dict):
        raise invalid("headers must be an object")
    headers: JsonObject = {}
    for name, content in source.items():
        key = name.lower()
        if (
            key in headers
            or key in {"host", "transfer-encoding"}
            or not name
            or not isinstance(content, str)
            or any(character in name + content for character in "\r\n\0")
        ):
            raise invalid("Invalid transfer header")
        headers[key] = content
    return {"kind": "url", "url": url, "headers": headers}


class FileManager:
    """Own transfers independently of the RPC connection that initiated them.

    The daemon lends one HTTP client configured with trust_env=False and no
    redirects. File and SQLite operations share the store's bounded worker.
    """

    def __init__(self, store: ExecutionStore, *, http_client: httpx2.AsyncClient) -> None:
        self.store = store
        self._http = http_client
        self._active: dict[tuple[str, str], _Transfer] = {}
        self._operations: set[asyncio.Task[JsonValue]] = set()
        self._begin_lock = asyncio.Lock()
        self._closing = False
        self._reaper: asyncio.Task[None] | None = None

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def initialize(self) -> None:
        """Fail interrupted transfers before accepting requests; never replay URLs."""
        while records := await self.store.unfinished_transfers():
            for record in records:
                if record.staging_path is not None:
                    await self.store.io.run(Path(record.staging_path).unlink, True)
                    record.staging_path = None
                record.info["state"] = "failed"
                record.info["error"] = {
                    "kind": "io_error",
                    "message": "Daemon interrupted transfer",
                }
                await self.store.save_transfer(record)
        self._reaper = asyncio.create_task(self._expire(), name="file-expiry")

    async def aclose(self) -> None:
        self._closing = True
        if self._reaper is not None:
            self._reaper.cancel()
            await asyncio.gather(self._reaper, return_exceptions=True)
        operations = list(self._operations)
        for task in operations:
            task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        for entry in list(self._active.values()):
            await self._abort(entry)

    async def abort_session(self, session_id: str) -> None:
        async with self._begin_lock:
            for entry in list(self._active.values()):
                if entry.record.session_id == session_id:
                    await self._abort(entry)

    async def _expire(self) -> None:
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            for entry in list(self._active.values()):
                if now - entry.last_activity >= TRANSFER_IDLE_SECONDS:
                    await self._abort(entry)

    async def handle(self, method: str, params: JsonParams) -> JsonValue:
        if self._closing:
            raise error("offline", "File manager is closing")
        if not isinstance(params, dict):
            raise invalid()
        if len(self._operations) >= 64:
            raise error("resource_limit", "Too many file operations")
        task = asyncio.create_task(self._dispatch(method, params), name="file-operation")
        self._operations.add(task)

        def finished(done: asyncio.Task[JsonValue]) -> None:
            self._operations.discard(done)
            if not done.cancelled():
                done.exception()  # A disconnected caller may no longer await it.

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _dispatch(self, method: str, params: JsonObject) -> JsonValue:
        if method in {"file.push", "file.pull"}:
            return await self._begin(method, params)
        if method not in {"file.chunk", "file.finish", "file.abort"}:
            raise RpcError(-32601, "Method not found")
        required = {"session_id", "transfer_id"}
        optional = {"wait_ms"} if method == "file.finish" else set()
        if method == "file.chunk":
            required.add("offset")
            optional = {"data_base64", "max_bytes"}
        fields(params, required, optional)
        session_id = session_identifier(params["session_id"])
        transfer_id = identifier(params["transfer_id"], "transfer_id")
        record = await self.store.get_transfer(session_id, transfer_id)
        if record is None:
            raise error("not_found", "Transfer not found")
        entry = self._active.get((session_id, transfer_id))
        if method == "file.abort":
            if entry is not None:
                await self._abort(entry)
                return {"aborted": True}
            return {"aborted": record.info["state"] == "aborted"}
        if method == "file.finish":
            wait_ms = integer(params.get("wait_ms", 1000), "wait_ms", maximum=30_000)
            if entry is None:
                return record.info
            entry.last_activity = time.monotonic()
            if entry.kind == "url":
                if wait_ms:
                    try:
                        async with asyncio.timeout(wait_ms / 1000):
                            await entry.done.wait()
                    except TimeoutError:
                        pass
                return dict(entry.record.info)
            async with entry.lock:
                if not entry.done.is_set():
                    try:
                        await self._commit(entry)
                    except (OSError, RpcError) as exc:
                        await self._fail(entry, exc)
                return dict(entry.record.info)
        if entry is None or entry.done.is_set():
            raise error("conflict", "Transfer is not open")
        if entry.kind != "websocket":
            raise error("conflict", "URL transfers do not accept chunks")
        async with entry.lock:
            if entry.done.is_set():
                raise error("conflict", "Transfer is not open")
            entry.last_activity = time.monotonic()
            try:
                return await self._chunk(entry, params)
            except OSError as exc:
                await self._fail(entry, exc)
                raise error("io_error", "File chunk failed") from exc
            except RpcError as exc:
                if isinstance(exc.data, dict) and exc.data.get("reason") == "file_changed":
                    await self._fail(entry, exc)
                raise

    async def _begin(self, method: str, params: JsonObject) -> JsonObject:
        required = {"session_id", "transfer_id", "path", "transport"}
        if method == "file.push":
            required.add("size")
        fields(params, required, {"sha256"} if method == "file.push" else set())
        session_id = session_identifier(params["session_id"])
        transfer_id = identifier(params["transfer_id"], "transfer_id")
        transport = _transport(params["transport"])
        supplied_path = string(params["path"], "path")
        direction = "push" if method == "file.push" else "pull"
        size = integer(params["size"], "size") if direction == "push" else 0
        checksum = params.get("sha256")
        if checksum is not None and (
            not isinstance(checksum, str)
            or len(checksum) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in checksum)
        ):
            raise invalid("sha256 must be 64 hexadecimal characters")
        checksum = checksum.lower() if isinstance(checksum, str) else None
        async with self._begin_lock:
            cwd = await self.store.session_cwd(session_id, require_token=True)
            path = Path(os.path.normpath(cwd / supplied_path))  # noqa: ASYNC240 - lexical, no I/O
            digest = fingerprint(
                {
                    "direction": direction,
                    "path": str(path),
                    "transport": transport,
                    "size": size,
                    "sha256": checksum,
                }
            )
            existing = await self.store.get_transfer(session_id, transfer_id)
            if existing is not None:
                if existing.fingerprint != digest:
                    raise error("conflict", "Transfer ID has different parameters")
                active = self._active.get((session_id, transfer_id))
                return dict(active.record.info if active is not None else existing.info)
            if len(self._active) >= MAX_TRANSFERS:
                raise error("resource_limit", "Too many active transfers")
            info: JsonObject = {
                "session_id": session_id,
                "transfer_id": transfer_id,
                "direction": direction,
                "state": "open" if transport["kind"] == "websocket" else "running",
                "offset": 0,
                "size": size,
                "sha256": checksum,
                "error": None,
            }
            staging = None
            if direction == "push":
                suffix = hashlib.sha256(session_id.encode()).hexdigest()[:16]
                staging = str(path.parent / f".kapy-transfer-{suffix}-{transfer_id}.tmp")
            record = TransferRecord(session_id, transfer_id, digest, str(path), staging, info)
            entry = _Transfer(record, cast(str, transport["kind"]))
            try:
                await self.store.save_transfer(record)
                await self.store.io.run(self._open, entry)
                if direction == "push":
                    if any(
                        other is not entry and other.destination == entry.destination
                        for other in self._active.values()
                    ):
                        raise error("conflict", "Another push is writing this destination")
                    if checksum is not None:
                        entry.digest = hashlib.sha256()
                await self.store.save_transfer(record)
                if entry.kind == "url":
                    self._url_headers(
                        entry, transport
                    )  # Fail invalid lengths before background I/O.
                # Publish only fully opened descriptors; a concurrent chunk for
                # the durable intent must not operate on a half-open transfer.
                self._active[(session_id, transfer_id)] = entry
                if entry.kind == "url":
                    entry.task = asyncio.create_task(
                        self._url_transfer(entry, transport), name="file-url"
                    )
                return dict(info)
            except asyncio.CancelledError:
                await self._terminal(entry, "aborted")
                raise
            except (OSError, RpcError) as exc:
                await self._fail(entry, exc)
                if isinstance(exc, RpcError):
                    raise
                return dict(info)

    @staticmethod
    def _open(entry: _Transfer) -> None:
        path = Path(entry.record.path)
        if entry.record.info["direction"] == "push":
            entry.parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            parent_info = os.fstat(entry.parent_fd)
            entry.destination = (parent_info.st_dev, parent_info.st_ino, path.name)
            try:
                target = os.stat(path.name, dir_fd=entry.parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                target = None
            if target is not None and not stat.S_ISREG(target.st_mode):
                raise error("conflict", "Push target must be a regular file or absent")
            assert entry.record.staging_path is not None
            entry.fd = os.open(
                Path(entry.record.staging_path).name,
                os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
                dir_fd=entry.parent_fd,
            )
        else:
            if not stat.S_ISREG(path.stat().st_mode):
                raise error("conflict", "Pull source must be a regular file")
            entry.fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
            source = os.fstat(entry.fd)
            if not stat.S_ISREG(source.st_mode):
                raise error("conflict", "Pull source must be a regular file")
            entry.signature = _signature(source)
            entry.record.info["size"] = source.st_size

    @staticmethod
    def _read(entry: _Transfer, offset: int, size: int) -> bytes:
        if _signature(os.fstat(entry.fd)) != entry.signature:
            raise error("conflict", "Pull source changed", reason="file_changed")
        data = os.pread(entry.fd, size, offset)
        if _signature(os.fstat(entry.fd)) != entry.signature:
            raise error("conflict", "Pull source changed", reason="file_changed")
        if len(data) != min(size, cast(int, entry.record.info["size"]) - offset):
            raise error("conflict", "Pull source changed", reason="file_changed")
        return data

    @staticmethod
    def _write(entry: _Transfer, offset: int, data: bytes) -> None:
        written = 0
        while written < len(data):
            count = os.pwrite(entry.fd, memoryview(data)[written:], offset + written)
            if count == 0:
                raise OSError("File write made no progress")
            written += count

    async def _chunk(self, entry: _Transfer, params: JsonObject) -> JsonObject:
        info = entry.record.info
        offset = integer(params["offset"], "offset", maximum=cast(int, info["size"]))
        if info["direction"] == "pull":
            if "data_base64" in params:
                raise invalid("Pull chunks do not accept data_base64")
            maximum = integer(params.get("max_bytes", CHUNK_SIZE), "max_bytes", 1, CHUNK_SIZE)
            data = await self.store.io.run(self._read, entry, offset, maximum)
            info["offset"] = max(cast(int, info["offset"]), offset + len(data))
            await self.store.save_transfer(entry.record)
            return byte_chunk(
                data, offset, cast(int, info["size"]), eof=offset + len(data) == info["size"]
            )
        if "data_base64" not in params or "max_bytes" in params:
            raise invalid("Push chunks require data_base64 and do not accept max_bytes")
        data = decode_chunk(params["data_base64"])
        current = cast(int, info["offset"])
        if offset == entry.last_offset and len(data) == entry.last_size:
            previous = await self.store.io.run(os.pread, entry.fd, len(data), offset)
            if previous == data:
                return {"next": current}
            raise error("conflict", "Repeated chunk contains different bytes")
        if offset != current:
            raise error("conflict", "Push chunk offset does not match", next=current)
        if offset + len(data) > cast(int, info["size"]):
            raise invalid("Push chunk exceeds declared file size")
        await self.store.io.run(self._write, entry, offset, data)
        if entry.digest is not None:
            entry.digest.update(data)
        entry.last_offset, entry.last_size = offset, len(data)
        info["offset"] = offset + len(data)
        await self.store.save_transfer(entry.record)
        return {"next": info["offset"]}

    def _url_headers(self, entry: _Transfer, transport: JsonObject) -> dict[str, str]:
        headers = dict(cast(dict[str, str], transport["headers"]))
        if entry.record.info["direction"] == "push":
            if "content-length" in headers:
                raise invalid("GET transfers cannot set request Content-Length")
            headers["accept-encoding"] = "identity"
        else:
            length = str(entry.record.info["size"])
            if headers.get("content-length", length) != length:
                raise invalid("PUT Content-Length must match the source size")
            headers["content-length"] = length
        return headers

    async def _url_transfer(self, entry: _Transfer, transport: JsonObject) -> None:
        try:
            headers = self._url_headers(entry, transport)
            direction = entry.record.info["direction"]
            if direction == "push":
                async with self._http.stream(
                    "GET",
                    cast(str, transport["url"]),
                    headers=headers,
                    follow_redirects=False,
                    timeout=httpx2.Timeout(60),
                ) as response:
                    await self._check_http(response)
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise error("io_error", "GET response uses unsupported content encoding")
                    async for data in response.aiter_raw(CHUNK_SIZE):
                        entry.last_activity = time.monotonic()
                        offset = cast(int, entry.record.info["offset"])
                        if offset + len(data) > cast(int, entry.record.info["size"]):
                            raise error("io_error", "GET exceeded declared file size")
                        await self.store.io.run(self._write, entry, offset, data)
                        if entry.digest is not None:
                            entry.digest.update(data)
                        entry.record.info["offset"] = offset + len(data)
                        await self.store.save_transfer(entry.record)
            else:
                async with self._http.stream(
                    "PUT",
                    cast(str, transport["url"]),
                    headers=headers,
                    content=self._upload(entry),
                    follow_redirects=False,
                    timeout=httpx2.Timeout(60),
                ) as response:
                    await self._check_http(response)
                    async for _ in response.aiter_raw(CHUNK_SIZE):
                        entry.last_activity = time.monotonic()
            async with entry.lock:
                await self._commit(entry)
        except asyncio.CancelledError:
            async with entry.lock:
                await self._terminal(entry, "aborted")
            raise
        except (OSError, RpcError, httpx2.HTTPError, ValueError) as exc:
            async with entry.lock:
                await self._fail(entry, exc)

    async def _upload(self, entry: _Transfer) -> AsyncIterator[bytes]:
        while cast(int, entry.record.info["offset"]) < cast(int, entry.record.info["size"]):
            offset = cast(int, entry.record.info["offset"])
            data = await self.store.io.run(self._read, entry, offset, CHUNK_SIZE)
            yield data
            entry.record.info["offset"] = offset + len(data)
            entry.last_activity = time.monotonic()
            await self.store.save_transfer(entry.record)

    @staticmethod
    async def _check_http(response: httpx2.Response) -> None:
        if 200 <= response.status_code < 300:
            return
        # Do not retain provider bodies: they may reflect a signed URL or token.
        async for _ in response.aiter_raw(4096):
            break
        raise error("io_error", f"Transfer HTTP status {response.status_code}")

    async def _commit(self, entry: _Transfer) -> None:
        info = entry.record.info
        if info["direction"] == "push":
            if info["offset"] != info["size"]:
                raise error("conflict", "Push is incomplete")
            if entry.digest is not None and entry.digest.hexdigest() != info["sha256"]:
                raise error("conflict", "Push checksum does not match")

            def commit() -> None:
                assert entry.record.staging_path is not None
                target = Path(entry.record.path).name
                try:
                    info = os.stat(target, dir_fd=entry.parent_fd, follow_symlinks=False)
                except FileNotFoundError:
                    info = None
                if info is not None and not stat.S_ISREG(info.st_mode):
                    raise error("conflict", "Push target changed to a non-regular file")
                os.fsync(entry.fd)
                os.replace(
                    Path(entry.record.staging_path).name,
                    target,
                    src_dir_fd=entry.parent_fd,
                    dst_dir_fd=entry.parent_fd,
                )
                os.fsync(entry.parent_fd)

            await self.store.io.run(commit)
            entry.record.staging_path = None
        else:
            if entry.kind == "url" and info["offset"] != info["size"]:
                raise error("io_error", "PUT did not send the complete source")
            await self.store.io.run(self._read, entry, 0, 0)
        await self._terminal(entry, "complete")

    async def _fail(self, entry: _Transfer, exc: BaseException) -> None:
        detail: JsonObject = {"kind": "io_error", "message": "File transfer failed"}
        if isinstance(exc, RpcError):
            detail = {
                "kind": "conflict" if exc.code == -32009 else "io_error",
                "message": exc.message,
            }
        await self._terminal(entry, "failed", detail)

    async def _terminal(
        self, entry: _Transfer, state: str, detail: JsonObject | None = None
    ) -> None:
        if entry.done.is_set():
            return

        def close() -> None:
            if entry.fd >= 0:
                os.close(entry.fd)
                entry.fd = -1
            try:
                if entry.record.staging_path is not None:
                    if entry.parent_fd >= 0:
                        try:
                            os.unlink(Path(entry.record.staging_path).name, dir_fd=entry.parent_fd)
                        except FileNotFoundError:
                            pass
                    else:
                        Path(entry.record.staging_path).unlink(missing_ok=True)
            finally:
                if entry.parent_fd >= 0:
                    os.close(entry.parent_fd)
                    entry.parent_fd = -1

        await self.store.io.run(close)
        entry.record.staging_path = None
        entry.record.info["state"] = state
        entry.record.info["error"] = detail
        await self.store.save_transfer(entry.record)
        self._active.pop((entry.record.session_id, entry.record.transfer_id), None)
        entry.done.set()

    async def _abort(self, entry: _Transfer) -> None:
        if entry.task is not None and not entry.task.done():
            entry.task.cancel()
            await asyncio.gather(entry.task, return_exceptions=True)
        async with entry.lock:
            await self._terminal(entry, "aborted")

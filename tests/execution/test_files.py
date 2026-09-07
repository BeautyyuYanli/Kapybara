import asyncio
import base64
import hashlib
import os
import resource
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
import pytest_asyncio

from kapy.execution import resolve_paths
from kapy.execution.files import FileManager
from kapy.execution.store import ExecutionStore
from kapy.rpc import JsonObject, RpcError


def roots(path: Path):
    return resolve_paths(state_dir=path / "state", data_dir=path / "data", runtime_dir=path / "run")


@pytest_asyncio.fixture
async def files(tmp_path: Path) -> AsyncIterator[tuple[FileManager, ExecutionStore, Path]]:
    async with ExecutionStore(roots(tmp_path), "machine") as store:
        await store.ensure_session("one", "session-secret")
        await store.ensure_session("two", "other-secret")
        async with httpx2.AsyncClient(trust_env=False, follow_redirects=False) as client:
            manager = FileManager(store, http_client=client)
            await manager.initialize()
            try:
                yield manager, store, await store.session_cwd("one")
            finally:
                await manager.aclose()


def begin_params(path: str, *, size: int | None = None, session: str = "one") -> JsonObject:
    params: JsonObject = {
        "session_id": session,
        "transfer_id": str(uuid4()),
        "path": path,
        "transport": {"kind": "websocket"},
    }
    if size is not None:
        params["size"] = size
    return params


def ref(params: JsonObject) -> JsonObject:
    return {"session_id": params["session_id"], "transfer_id": params["transfer_id"]}


@pytest.mark.asyncio
async def test_websocket_atomic_push_duplicates_and_session_isolation(files) -> None:
    manager, store, cwd = files
    target = cwd / "target"
    await asyncio.to_thread(target.write_bytes, b"old")
    data = b"replacement\0bytes"
    params = begin_params("target", size=len(data))
    params["sha256"] = hashlib.sha256(data).hexdigest()
    assert (await manager.handle("file.push", params))["state"] == "open"
    chunk = {**ref(params), "offset": 0, "data_base64": base64.b64encode(data).decode()}
    assert await manager.handle("file.chunk", chunk) == {"next": len(data)}
    assert await manager.handle("file.chunk", chunk) == {"next": len(data)}
    assert await asyncio.to_thread(target.read_bytes) == b"old"
    with pytest.raises(RpcError) as caught:
        await manager.handle(
            "file.chunk", {**chunk, "data_base64": base64.b64encode(b"x" * len(data)).decode()}
        )
    assert caught.value.code == -32009
    with pytest.raises(RpcError) as caught:
        await manager.handle("file.finish", {**ref(params), "session_id": "two"})
    assert caught.value.code == -32004
    done = await manager.handle("file.finish", ref(params))
    assert done["state"] == "complete"
    assert await asyncio.to_thread(target.read_bytes) == data
    assert (await manager.handle("file.push", params))["state"] == "complete"
    assert not list(cwd.glob(".kapy-transfer-*"))
    with pytest.raises(RpcError) as caught:
        await manager.handle("file.push", {**params, "size": 100})
    assert caught.value.code == -32009


@pytest.mark.asyncio
async def test_push_failure_abort_conflicts_and_symlinks_preserve_existing_file(files) -> None:
    manager, store, cwd = files
    target = cwd / "target"
    await asyncio.to_thread(target.write_bytes, b"original")
    params = begin_params("target", size=1)
    params["sha256"] = "0" * 64
    await manager.handle("file.push", params)
    with pytest.raises(RpcError):
        await manager.handle("file.push", begin_params("target", size=1))
    await manager.handle("file.chunk", {**ref(params), "offset": 0, "data_base64": "eA=="})
    assert (await manager.handle("file.finish", ref(params)))["state"] == "failed"
    assert await asyncio.to_thread(target.read_bytes) == b"original"
    abort = begin_params("new-target", size=100)
    await manager.handle("file.push", abort)
    assert await manager.handle("file.abort", ref(abort)) == {"aborted": True}
    assert await manager.handle("file.abort", ref(abort)) == {"aborted": True}
    (cwd / "symlink").symlink_to(target)
    with pytest.raises(RpcError):
        await manager.handle("file.push", begin_params("symlink", size=1))
    assert await asyncio.to_thread(target.read_bytes) == b"original"
    assert not list(cwd.glob(".kapy-transfer-*"))


@pytest.mark.asyncio
async def test_pull_random_cursor_and_source_mutation_fail_explicitly(files) -> None:
    manager, store, cwd = files
    target = cwd / "source"
    await asyncio.to_thread(target.write_bytes, b"0123456789")
    params = begin_params("source")
    await manager.handle("file.pull", params)
    value = await manager.handle("file.chunk", {**ref(params), "offset": 7, "max_bytes": 3})
    assert base64.b64decode(value["data_base64"]) == b"789"
    assert value["eof"] is True
    value = await manager.handle("file.chunk", {**ref(params), "offset": 0, "max_bytes": 2})
    assert value["eof"] is False
    await asyncio.to_thread(target.write_bytes, b"changed")
    with pytest.raises(RpcError) as caught:
        await manager.handle("file.chunk", {**ref(params), "offset": 0})
    assert isinstance(caught.value.data, dict)
    assert caught.value.data["reason"] == "file_changed"
    assert (await manager.handle("file.finish", ref(params)))["state"] == "failed"
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_transfer_capacity_and_fifo_do_not_block(files) -> None:
    manager, store, cwd = files
    (cwd / "directory").mkdir()
    os.mkfifo(cwd / "fifo")
    for name in ("directory", "fifo"):
        with pytest.raises(RpcError):
            await asyncio.wait_for(manager.handle("file.pull", begin_params(name)), 1)
    for index in range(8):
        await manager.handle("file.push", begin_params(f"target-{index}", size=1))
    with pytest.raises(RpcError) as caught:
        await manager.handle("file.push", begin_params("overflow", size=1))
    assert caught.value.code == -32020
    await manager.abort_session("one")
    assert manager.active_count == 0
    assert not list(cwd.glob(".kapy-transfer-*"))


def file_hash(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


@pytest.mark.asyncio
async def test_64mib_websocket_push_pull_bounded_memory(files) -> None:
    manager, store, cwd = files
    size = 64 * 1024 * 1024
    chunk = bytes(range(256)) * 256
    encoded = base64.b64encode(chunk).decode()
    expected = hashlib.sha256()
    for _ in range(size // len(chunk)):
        expected.update(chunk)
    params = begin_params("large", size=size)
    params["sha256"] = expected.hexdigest()
    started = time.monotonic()
    baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    await manager.handle("file.push", params)
    for offset in range(0, size, len(chunk)):
        await manager.handle(
            "file.chunk", {**ref(params), "offset": offset, "data_base64": encoded}
        )
    assert (await manager.handle("file.finish", ref(params)))["state"] == "complete"
    assert await asyncio.to_thread(file_hash, cwd / "large") == expected.hexdigest()
    pull = begin_params("large")
    await manager.handle("file.pull", pull)
    received = hashlib.sha256()
    for offset in range(0, size, len(chunk)):
        result = await manager.handle("file.chunk", {**ref(pull), "offset": offset})
        received.update(base64.b64decode(result["data_base64"]))
    assert received.hexdigest() == expected.hexdigest()
    assert (await manager.handle("file.finish", ref(pull)))["state"] == "complete"
    growth_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - baseline
    print(
        f"64 MiB WS push+pull: {time.monotonic() - started:.2f}s, peak RSS growth {growth_kib} KiB"
    )
    assert growth_kib < 32 * 1024


@pytest.mark.asyncio
async def test_crashed_daemon_marks_transfer_failed_and_removes_staging(tmp_path: Path) -> None:
    script = """
import asyncio, base64, os, sys
from pathlib import Path
from uuid import UUID
import httpx2
from kapy.execution import resolve_paths
from kapy.execution.store import ExecutionStore
from kapy.execution.files import FileManager
async def main():
    root = Path(sys.argv[1])
    paths = resolve_paths(state_dir=root/'state', data_dir=root/'data', runtime_dir=root/'run')
    async with ExecutionStore(paths, 'machine') as store:
        await store.ensure_session('one', 'never-persist-this-token')
        async with httpx2.AsyncClient(trust_env=False) as client:
            manager = FileManager(store, http_client=client)
            await manager.initialize()
            params = {'session_id':'one','transfer_id':sys.argv[2],'path':'target','size':10,
                      'transport':{'kind':'websocket'}}
            await manager.handle('file.push', params)
            await manager.handle('file.chunk', {'session_id':'one','transfer_id':sys.argv[2],
                'offset':0,'data_base64':'YWJj'})
            os._exit(17)
asyncio.run(main())
"""
    transfer_id = str(uuid4())
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, str(tmp_path), transfer_id
    )
    assert await asyncio.wait_for(process.wait(), 10) == 17
    paths = roots(tmp_path)
    assert list(paths.session_cwd("one").glob(".kapy-transfer-*"))
    async with ExecutionStore(paths, "machine") as store:
        async with httpx2.AsyncClient(trust_env=False) as client:
            manager = FileManager(store, http_client=client)
            await manager.initialize()
            try:
                result = await manager.handle(
                    "file.finish", {"session_id": "one", "transfer_id": transfer_id}
                )
                assert isinstance(result, dict)
                assert result["state"] == "failed"
                assert result["offset"] == 3
                assert not list(paths.session_cwd("one").glob(".kapy-transfer-*"))
                assert not store.authenticate_session("one", "never-persist-this-token")
            finally:
                await manager.aclose()


@asynccontextmanager
async def transfer_server(size: int, mode: str = "ok", gate: asyncio.Event | None = None):
    uploaded: dict[str, object] = {}
    received = asyncio.Event()
    handlers: set[asyncio.Task] = set()
    block = bytes(range(256)) * 256

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        handlers.add(task)
        try:
            head = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
            lines = head.split("\r\n")
            method, _, _ = lines[0].split(" ")
            headers = dict(line.lower().split(": ", 1) for line in lines[1:] if line)
            assert "session-secret" not in head
            received.set()
            if mode == "redirect":
                writer.write(b"HTTP/1.1 302 Found\r\nLocation: /other\r\nContent-Length: 0\r\n\r\n")
            elif mode == "error":
                writer.write(b"HTTP/1.1 500 Error\r\nContent-Length: 9\r\n\r\nsecret123")
            elif method == "GET":
                assert headers["accept-encoding"] == "identity"
                encoding = "Content-Encoding: gzip\r\n" if mode == "encoded" else ""
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {size}\r\n{encoding}\r\n".encode()
                )
                await writer.drain()
                if gate is not None:
                    await gate.wait()
                total = min(size, len(block)) if mode == "short" else size
                for offset in range(0, total, len(block)):
                    writer.write(block[: min(len(block), total - offset)])
                    await writer.drain()
            else:
                expected_size = int(headers["content-length"])
                assert expected_size == size
                digest = hashlib.sha256()
                consumed = 0
                while consumed < expected_size:
                    data = await reader.read(min(65_536, expected_size - consumed))
                    if not data:
                        break
                    digest.update(data)
                    consumed += len(data)
                uploaded.update(size=consumed, sha256=digest.hexdigest())
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")
            await writer.drain()
        except ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionResetError, BrokenPipeError:
                pass
            handlers.discard(task)

    async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        try:
            yield f"http://127.0.0.1:{port}/object?signature=test-url-secret", uploaded, received
        finally:
            for task in list(handlers):
                task.cancel()
            await asyncio.gather(*handlers, return_exceptions=True)


@pytest.mark.asyncio
async def test_64mib_presigned_get_put_stream_and_do_not_persist_url(files) -> None:
    manager, store, cwd = files
    size = 64 * 1024 * 1024
    started = time.monotonic()
    baseline = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    async with transfer_server(size) as (url, uploaded, received):
        push = begin_params("url-large", size=size)
        push["transport"] = {"kind": "url", "url": url, "headers": {"X-Test": "test-header-secret"}}
        await manager.handle("file.push", push)
        result = await manager.handle("file.finish", {**ref(push), "wait_ms": 30_000})
        assert result["state"] == "complete"
        expected_hash = await asyncio.to_thread(file_hash, cwd / "url-large")
        source_hash = hashlib.sha256()
        for _ in range(size // 65_536):
            source_hash.update(bytes(range(256)) * 256)
        assert expected_hash == source_hash.hexdigest()
        pull = begin_params("url-large")
        pull["transport"] = {"kind": "url", "url": url}
        await manager.handle("file.pull", pull)
        assert (await manager.handle("file.finish", {**ref(pull), "wait_ms": 30_000}))[
            "state"
        ] == "complete"
        assert uploaded == {"size": size, "sha256": expected_hash}
        for item in store.paths.state_dir.iterdir():
            if item.is_file():
                data = await asyncio.to_thread(item.read_bytes)
                assert b"test-url-secret" not in data
                assert b"test-header-secret" not in data
    growth_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss - baseline
    print(
        f"64 MiB URL GET+PUT: {time.monotonic() - started:.2f}s, peak RSS growth {growth_kib} KiB"
    )
    assert growth_kib < 32 * 1024


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["short", "redirect", "error", "encoded"])
async def test_url_failures_leave_original_and_remove_staging(files, mode: str) -> None:
    manager, store, cwd = files
    target = cwd / "target"
    await asyncio.to_thread(target.write_bytes, b"original")
    async with transfer_server(128 * 1024, mode) as (url, uploaded, received):
        params = begin_params("target", size=128 * 1024)
        params["transport"] = {"kind": "url", "url": url}
        await manager.handle("file.push", params)
        result = await manager.handle("file.finish", {**ref(params), "wait_ms": 5000})
        assert result["state"] == "failed"
        assert "secret123" not in str(result)
        assert await asyncio.to_thread(target.read_bytes) == b"original"
        assert not list(cwd.glob(".kapy-transfer-*"))


@pytest.mark.asyncio
async def test_cancelled_observer_does_not_cancel_url_transfer(files) -> None:
    manager, store, cwd = files
    gate = asyncio.Event()
    async with transfer_server(65_536, gate=gate) as (url, uploaded, received):
        params = begin_params("target", size=65_536)
        params["transport"] = {"kind": "url", "url": url}
        await manager.handle("file.push", params)
        await asyncio.wait_for(received.wait(), 1)
        observer = asyncio.create_task(
            manager.handle("file.finish", {**ref(params), "wait_ms": 30_000})
        )
        await asyncio.sleep(0)
        observer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await observer
        assert manager.active_count == 1
        gate.set()
        assert (await manager.handle("file.finish", {**ref(params), "wait_ms": 5000}))[
            "state"
        ] == "complete"
        assert (cwd / "target").stat().st_size == 65_536


@pytest.mark.asyncio
async def test_disk_write_failure_is_explicit_and_preserves_target(files, monkeypatch) -> None:
    manager, store, cwd = files
    target = cwd / "target"
    await asyncio.to_thread(target.write_bytes, b"old")
    params = begin_params("target", size=1)
    await manager.handle("file.push", params)

    def disk_full(*args) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(manager, "_write", disk_full)
    with pytest.raises(RpcError) as caught:
        await manager.handle("file.chunk", {**ref(params), "offset": 0, "data_base64": "eA=="})
    assert caught.value.code == -32021
    assert (await manager.handle("file.finish", ref(params)))["state"] == "failed"
    assert await asyncio.to_thread(target.read_bytes) == b"old"
    assert not list(cwd.glob(".kapy-transfer-*"))


@pytest.mark.asyncio
async def test_chunk_during_pending_open_cannot_corrupt_transfer(files, monkeypatch) -> None:
    manager, store, cwd = files
    opening = threading.Event()
    proceed = threading.Event()
    original = manager._open

    def delayed_open(entry) -> None:
        opening.set()
        if not proceed.wait(5):
            raise RuntimeError("Test failed to release file open")
        original(entry)

    monkeypatch.setattr(manager, "_open", delayed_open)
    params = begin_params("target", size=1)
    begin = asyncio.create_task(manager.handle("file.push", params))
    try:
        assert await asyncio.to_thread(opening.wait, 1)
        with pytest.raises(RpcError) as caught:
            await manager.handle("file.chunk", {**ref(params), "offset": 0, "data_base64": "eA=="})
        assert caught.value.code == -32009
    finally:
        proceed.set()
    assert (await begin)["state"] == "open"
    await manager.handle("file.chunk", {**ref(params), "offset": 0, "data_base64": "eA=="})
    assert (await manager.handle("file.finish", ref(params)))["state"] == "complete"
    assert await asyncio.to_thread((cwd / "target").read_bytes) == b"x"

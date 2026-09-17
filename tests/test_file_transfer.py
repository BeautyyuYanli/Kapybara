"""Real files and loopback HTTP; run in the disposable Docker machine."""

import asyncio
import gzip
import os
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing, asynccontextmanager
from pathlib import Path

import anyio
import httpx2
import pytest

from kapy.file_transfer import download_file, read_file, upload_file, write_file

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not Path("/.dockerenv").exists(),
        reason="Real file tests require the dedicated Docker machine",
    ),
]


@asynccontextmanager
async def serve(
    handler: Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]],
) -> AsyncGenerator[str]:
    tasks: set[asyncio.Task[None]] = set()
    errors: list[Exception] = []

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await handler(reader, writer)
        except Exception as exc:
            errors.append(exc)
        finally:
            writer.close()
            await writer.wait_closed()

    def accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks.add(asyncio.create_task(connection(reader, writer)))

    async with await asyncio.start_server(accept, "127.0.0.1", 0) as server:
        try:
            yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
    assert not errors


async def request_head(reader: asyncio.StreamReader) -> tuple[str, dict[str, str]]:
    lines = (await reader.readuntil(b"\r\n\r\n")).decode("ascii").split("\r\n")
    headers = dict(line.split(":", 1) for line in lines[1:] if line)
    return lines[0], {key.lower(): value.strip() for key, value in headers.items()}


def open_handles(path: Path) -> list[int]:
    handles = []
    for entry in Path("/proc/self/fd").iterdir():
        try:
            if entry.samefile(path):
                handles.append(int(entry.name))
        except FileNotFoundError:
            pass
    return handles


@pytest.mark.parametrize("empty", [False, True])
async def test_stream_roundtrip_and_empty_chunks(tmp_path: Path, empty: bool):
    source, target = tmp_path / "source", tmp_path / "target"
    data = b"" if empty else bytes(range(256)) * 1300
    source.write_bytes(data)
    target.write_bytes(b"old")
    stream = read_file(source)
    assert not open_handles(source)
    chunks = []
    async with aclosing(stream):
        async for chunk in stream:
            assert 0 < len(chunk) <= 65_536
            chunks.append(chunk)
    assert b"".join(chunks) == data
    assert not open_handles(source)

    async def content():
        for chunk in chunks:
            yield b""
            yield chunk
        yield b""

    async with aclosing(content()) as incoming:
        assert await write_file(target, incoming) == len(data)
    assert target.read_bytes() == data
    assert {entry.name async for entry in anyio.Path(tmp_path).iterdir()} == {"source", "target"}


async def test_reader_early_close_and_source_constraints(tmp_path: Path):
    source = tmp_path / "source"
    source.write_bytes(b"x" * 100_000)
    stream = read_file(source)
    await anext(stream)
    assert open_handles(source)
    await stream.aclose()
    assert not open_handles(source)
    with pytest.raises(FileNotFoundError):
        await anext(read_file(tmp_path / "missing"))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    async with asyncio.timeout(2):
        with pytest.raises(ValueError, match="regular"):
            await anext(read_file(fifo))


async def test_write_failure_preserves_target_and_borrowed_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "target"
    target.write_bytes(b"old")
    closed = False

    async def content():
        nonlocal closed
        try:
            yield b"new"
            yield b"later"
        finally:
            closed = True

    async def fail_write(self: anyio.AsyncFile[bytes], data: bytes) -> int:
        raise OSError("injected write failure")

    monkeypatch.setattr(anyio.AsyncFile, "write", fail_write)
    async with aclosing(content()) as incoming:
        with pytest.raises(OSError, match="injected"):
            await write_file(target, incoming)
        assert not closed
    assert closed
    assert target.read_bytes() == b"old"
    assert [entry.name async for entry in anyio.Path(tmp_path).iterdir()] == ["target"]


async def test_write_input_failure_and_missing_parent(tmp_path: Path):
    target = tmp_path / "target"
    target.write_bytes(b"old")

    async def failed_input():
        yield b"partial"
        raise RuntimeError("input failed")

    async with aclosing(failed_input()) as incoming:
        with pytest.raises(RuntimeError, match="input failed"):
            await write_file(target, incoming)
    assert target.read_bytes() == b"old"
    assert [entry.name async for entry in anyio.Path(tmp_path).iterdir()] == ["target"]
    async with aclosing(read_file(target)) as incoming:
        with pytest.raises(FileNotFoundError):
            await write_file(tmp_path / "missing" / "target", incoming)


@pytest.mark.parametrize("cancel_scope", [False, True])
async def test_write_cancellation_joins_blocking_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cancel_scope: bool
):
    target = tmp_path / "target"
    target.write_bytes(b"old")
    entered, resume = threading.Event(), threading.Event()
    finished = False

    async def slow_write(self: anyio.AsyncFile[bytes], data: bytes) -> int:
        def blocking_write() -> int:
            nonlocal finished
            entered.set()
            assert resume.wait(5)
            assert not self.wrapped.closed
            count = self.wrapped.write(data)
            finished = True
            return count

        return await anyio.to_thread.run_sync(blocking_write)

    async def content():
        yield b"new"

    scope = anyio.CancelScope()

    async def run():
        with scope:
            async with aclosing(content()) as incoming:
                await write_file(target, incoming)

    monkeypatch.setattr(anyio.AsyncFile, "write", slow_write)
    task = asyncio.create_task(run())
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        if cancel_scope:
            scope.cancel()
        else:
            task.cancel()
        await asyncio.sleep(0.05)
        assert not task.done()
    finally:
        resume.set()
    if cancel_scope:
        await task
    else:
        with pytest.raises(asyncio.CancelledError):
            await task
    assert finished
    assert target.read_bytes() == b"old"
    assert [entry.name async for entry in anyio.Path(tmp_path).iterdir()] == ["target"]


async def test_cancel_during_file_open_closes_acquired_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source"
    source.write_bytes(b"content")
    opened, resume = asyncio.Event(), asyncio.Event()
    original_open = anyio.open_file

    async def slow_open(*args, **kwargs):
        file = await original_open(*args, **kwargs)
        opened.set()
        await resume.wait()
        return file

    monkeypatch.setattr(anyio, "open_file", slow_open)
    stream = read_file(source)
    task = asyncio.create_task(anext(stream))
    await asyncio.wait_for(opened.wait(), 3)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not open_handles(source)


@pytest.mark.parametrize("empty", [False, True])
async def test_presigned_put_streams_with_fixed_length(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty: bool
):
    source = tmp_path / "source"
    data = b"" if empty else bytes(range(256)) * 4000
    source.write_bytes(data)
    first_received, disconnected = asyncio.Event(), asyncio.Event()
    original_read = anyio.AsyncFile.read
    reads = 0
    requests = []

    async def paced_read(self: anyio.AsyncFile[bytes], size: int = -1) -> bytes:
        nonlocal reads
        assert 0 < size <= 65_536
        if reads and not empty:
            await asyncio.wait_for(first_received.wait(), 3)
        reads += 1
        return await original_read(self, size)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        head, headers = await request_head(reader)
        requests.append(head)
        assert head == "PUT /object%2Fname?sig=a%2Bb&part=1 HTTP/1.1"
        assert headers["content-length"] == str(len(data))
        assert headers["x-signed"] == "preserved-value"
        assert "transfer-encoding" not in headers
        first = await reader.read(min(65_536, len(data)))
        first_received.set()
        remaining = await reader.readexactly(len(data) - len(first))
        assert first + remaining == data
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n")
        await writer.drain()
        # The transfer must not buffer or wait for an irrelevant PUT response body.
        assert await reader.read() == b""
        disconnected.set()

    monkeypatch.setattr(anyio.AsyncFile, "read", paced_read)
    headers = {"X-Signed": "preserved-value"}
    if empty:
        headers["cOnTeNt-LeNgTh"] = "0"
    async with serve(handler) as url, httpx2.AsyncClient(timeout=3) as client:
        assert await upload_file(
            client,
            source,
            url=url + "/object%2Fname?sig=a%2Bb&part=1",
            headers=headers,
        ) == len(data)
        await asyncio.wait_for(disconnected.wait(), 3)
        assert not client.is_closed
    assert len(requests) == 1
    assert not open_handles(source)


@pytest.mark.parametrize("headers", [{"Content-Length": "9"}, {"Transfer-Encoding": "chunked"}])
async def test_put_rejects_inconsistent_framing(tmp_path: Path, headers: dict[str, str]):
    source = tmp_path / "source"
    source.write_bytes(b"test")
    async with httpx2.AsyncClient() as client:
        with pytest.raises(ValueError):
            await upload_file(client, source, url="http://127.0.0.1:1/unused", headers=headers)
    assert not open_handles(source)


@pytest.mark.parametrize("encoding", [None, "gzip"])
async def test_download_preserves_raw_bytes_and_headers(tmp_path: Path, encoding: str | None):
    payload = gzip.compress(os.urandom(200_000))
    target = tmp_path / "target"
    target.write_bytes(b"old")

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        head, headers = await request_head(reader)
        assert head == "GET /object?sig=unchanged%2F HTTP/1.1"
        assert headers["accept-encoding"] == (encoding or "identity")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: "
            + str(len(payload)).encode()
            + b"\r\n\r\n"
        )
        for offset in range(0, len(payload), 137):
            writer.write(payload[offset : offset + 137])
            await writer.drain()

    headers = {"Accept-Encoding": encoding} if encoding else None
    async with serve(handler) as url, httpx2.AsyncClient(timeout=3) as client:
        assert await download_file(
            client, target, url=url + "/object?sig=unchanged%2F", headers=headers
        ) == len(payload)
        assert not client.is_closed
    assert target.read_bytes() == payload
    assert [entry.name async for entry in anyio.Path(tmp_path).iterdir()] == ["target"]


async def test_cancel_upload_closes_source_and_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source = tmp_path / "source"
    source.write_bytes(b"x" * 200_000)
    waiting, resume, disconnected = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_read = anyio.AsyncFile.read
    reads = 0

    async def paused_read(self: anyio.AsyncFile[bytes], size: int = -1) -> bytes:
        nonlocal reads
        reads += 1
        if reads > 1:
            waiting.set()
            await resume.wait()
        return await original_read(self, size)

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await request_head(reader)
        await reader.read()
        disconnected.set()

    monkeypatch.setattr(anyio.AsyncFile, "read", paused_read)
    async with serve(handler) as url, httpx2.AsyncClient(timeout=3) as client:
        task = asyncio.create_task(upload_file(client, source, url=url))
        await asyncio.wait_for(waiting.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        resume.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 3)
        await asyncio.wait_for(disconnected.wait(), 3)
        assert not client.is_closed
    assert not open_handles(source)


@pytest.mark.parametrize("status", ["403 Forbidden", "307 Temporary Redirect"])
async def test_http_errors_do_not_read_body_or_follow_redirects(tmp_path: Path, status: str):
    target = tmp_path / "target"
    target.write_bytes(b"old")
    calls = 0

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        nonlocal calls
        await request_head(reader)
        calls += 1
        writer.write(
            f"HTTP/1.1 {status}\r\nLocation: /elsewhere\r\nContent-Length: 1000000\r\n\r\n".encode()
        )
        await writer.drain()
        assert await reader.read() == b""

    async with (
        serve(handler) as url,
        httpx2.AsyncClient(timeout=3, follow_redirects=True) as client,
    ):
        with pytest.raises(httpx2.HTTPStatusError):
            await download_file(client, target, url=url)
        assert not client.is_closed
    assert calls == 1
    assert target.read_bytes() == b"old"
    assert [entry.name async for entry in anyio.Path(tmp_path).iterdir()] == ["target"]


async def test_truncated_download_does_not_replace_target(tmp_path: Path):
    target = tmp_path / "target"
    target.write_bytes(b"old")

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await request_head(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\npartial")
        await writer.drain()

    async with serve(handler) as url, httpx2.AsyncClient(timeout=3) as client:
        with pytest.raises(httpx2.RemoteProtocolError):
            await download_file(client, target, url=url)
    assert target.read_bytes() == b"old"
    assert [entry.name async for entry in anyio.Path(tmp_path).iterdir()] == ["target"]


async def test_cancel_download_closes_response_and_preserves_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    target = tmp_path / "target"
    target.write_bytes(b"old")
    written, disconnected = asyncio.Event(), asyncio.Event()
    original_write = anyio.AsyncFile.write

    async def observed_write(self: anyio.AsyncFile[bytes], data: bytes) -> int:
        count = await original_write(self, data)
        written.set()
        return count

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await request_head(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 1000000\r\n\r\n" + b"x" * 65_536)
        await writer.drain()
        assert await reader.read() == b""
        disconnected.set()

    monkeypatch.setattr(anyio.AsyncFile, "write", observed_write)
    async with serve(handler) as url, httpx2.AsyncClient(timeout=3) as client:
        task = asyncio.create_task(download_file(client, target, url=url))
        await asyncio.wait_for(written.wait(), 3)
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            # Cancellation must interrupt the pending body read, not wait for its
            # three-second HTTP timeout or for the server to send another chunk.
            await asyncio.wait_for(task, 1)
        await asyncio.wait_for(disconnected.wait(), 3)
        assert not client.is_closed
    assert target.read_bytes() == b"old"
    assert [entry.name async for entry in anyio.Path(tmp_path).iterdir()] == ["target"]


async def test_cancel_during_response_auto_close_releases_connection(tmp_path: Path):
    target = tmp_path / "target"
    target.write_bytes(b"old")
    closing, resume = asyncio.Event(), asyncio.Event()

    async def trace(name: str, info: dict[str, object]) -> None:
        if name == "http11.response_closed.started" and not closing.is_set():
            closing.set()
            await resume.wait()

    async def attach_trace(request: httpx2.Request) -> None:
        request.extensions["trace"] = trace

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        await request_head(reader)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 3\r\nConnection: close\r\n\r\nnew")
        await writer.drain()
        assert await reader.read() == b""

    async with (
        serve(handler) as url,
        httpx2.AsyncClient(
            timeout=1,
            limits=httpx2.Limits(max_connections=1),
            event_hooks={"request": [attach_trace]},
        ) as client,
    ):
        task = asyncio.create_task(download_file(client, target, url=url))
        try:
            await asyncio.wait_for(closing.wait(), 3)
            task.cancel()
            await asyncio.sleep(0.01)
            awaiting_close = not task.done()
        finally:
            resume.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert target.read_bytes() == b"old"
        assert [entry.name async for entry in anyio.Path(tmp_path).iterdir()] == ["target"]
        # One available pool slot proves the canceled response returned its lease.
        assert await download_file(client, target, url=url) == 3
        assert target.read_bytes() == b"new"
        assert awaiting_close

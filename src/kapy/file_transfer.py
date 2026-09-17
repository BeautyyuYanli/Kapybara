"""Stream local files and presigned HTTP requests without owning business resources.

These asyncio services borrow the HTTP client and input streams. Callers choose
paths, keep upload sources stable, and configure a client without default query
parameters, authentication or cookies that would change a signed request. There
are no retries, redirects, retained transfer records or background transfers.

File operations use AnyIO's worker threads. Cancellation joins an in-flight I/O
operation before releasing its file or response, including direct Task.cancel().
"""

import asyncio
import os
import stat
import tempfile
from collections.abc import AsyncGenerator, AsyncIterable, Coroutine, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx2

__all__ = ["download_file", "read_file", "upload_file", "write_file"]

_CHUNK_SIZE = 65_536


async def _finish_io[T](operation: Coroutine[Any, Any, T]) -> T:
    # AnyIO shields its threads from CancelScope cancellation, but not a direct
    # asyncio Task.cancel(). Join this one operation; never detach a transfer.
    task = asyncio.create_task(operation)
    cancelled: asyncio.CancelledError | None = None
    with anyio.CancelScope() as scope:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
                scope.shield = True
            except Exception:
                break
        if cancelled is not None:
            if not task.cancelled():
                task.exception()
            raise cancelled
    return task.result()


def _regular_opener(path: str, flags: int) -> int:
    fd = os.open(path, flags | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("Source must be a regular file")
        return fd
    except BaseException:
        os.close(fd)
        raise


@asynccontextmanager
async def _source(path: Path) -> AsyncGenerator[anyio.AsyncFile[bytes]]:
    file: anyio.AsyncFile[bytes] | None = None

    async def acquire() -> None:
        nonlocal file
        file = await anyio.open_file(path, "rb", opener=_regular_opener)

    try:
        # Assign inside the joined operation so cancellation cannot lose an open FD.
        await _finish_io(acquire())
        assert file is not None
        yield file
    finally:
        if file is not None:
            await _finish_io(file.aclose())


async def _chunks(file: anyio.AsyncFile[bytes]) -> AsyncGenerator[bytes]:
    while chunk := await _finish_io(file.read(_CHUNK_SIZE)):
        yield chunk


async def read_file(path: Path) -> AsyncGenerator[bytes]:
    """Lazily read a stable regular file in chunks of at most 64 KiB.

    Consume with ``async for``, without awaiting the generator. Consumers that
    stop early must call ``aclose()`` (for example via ``contextlib.aclosing``).
    File errors propagate as OSError; a non-regular source raises ValueError.
    """
    async with _source(path) as file:
        async for chunk in _chunks(file):
            yield chunk


async def write_file(path: Path, content: AsyncIterable[bytes]) -> int:
    """Write a borrowed byte stream, atomically replace path, and return its size.

    The parent directory must exist. Empty chunks are ignored. Failure or
    cancellation before replacement preserves the old target and removes the
    temporary file. Replacement is the commit point, with no crash recovery or
    rollback afterward. The caller remains responsible for closing content.
    """
    file: anyio.AsyncFile[bytes] | None = None
    temporary: Path | None = None

    async def acquire() -> None:
        nonlocal file, temporary
        raw = await anyio.to_thread.run_sync(
            lambda: tempfile.NamedTemporaryFile(
                mode="wb", dir=path.parent, prefix=".kapy-transfer-", delete=False
            )
        )
        temporary = Path(raw.name)
        file = anyio.wrap_file(raw)

    async def cleanup() -> None:
        try:
            if file is not None:
                await file.aclose()
        finally:
            if temporary is not None:
                await anyio.Path(temporary).unlink(missing_ok=True)

    try:
        await _finish_io(acquire())
        assert file is not None and temporary is not None
        count = 0
        async for chunk in content:
            if chunk:
                count += await _finish_io(file.write(chunk))
        await _finish_io(file.aclose())
        await anyio.lowlevel.checkpoint()
        await _finish_io(anyio.Path(temporary).replace(path))
        return count
    finally:
        await _finish_io(cleanup())


async def upload_file(
    client: httpx2.AsyncClient,
    local_path: Path,
    *,
    url: str,
    headers: Mapping[str, str] | None = None,
) -> int:
    """PUT a stable regular file to a presigned URL; return bytes sent on success.

    Content-Length comes from the opened file. A supplied length must match and
    Transfer-Encoding is not allowed. Signed header values are preserved. HTTP
    errors propagate without reading the error body. An unsuccessful or cancelled
    call may already have changed the remote object; no remote cleanup is done.
    """
    async with _source(local_path) as file:
        size = (await _finish_io(anyio.to_thread.run_sync(os.fstat, file.wrapped.fileno()))).st_size
        request_headers = httpx2.Headers(headers)
        length = request_headers.get("Content-Length")
        if length is None:
            request_headers["Content-Length"] = str(size)
        elif not length.isascii() or not length.isdecimal() or int(length) != size:
            raise ValueError("Content-Length must match the source size")
        request = client.build_request("PUT", url, headers=request_headers, content=_chunks(file))
        if "Transfer-Encoding" in request.headers:
            raise ValueError("PUT must use Content-Length, not Transfer-Encoding")
        response = await client.send(request, stream=True, follow_redirects=False)
        try:
            response.raise_for_status()
            return size
        finally:
            await _finish_io(response.aclose())


async def download_file(
    client: httpx2.AsyncClient,
    local_path: Path,
    *,
    url: str,
    headers: Mapping[str, str] | None = None,
) -> int:
    """GET raw response bytes into an atomically replaced file; return its size.

    Accept-Encoding defaults to identity unless supplied by the caller. Content
    encodings are never decoded. File and HTTP errors propagate; errors before
    replacement preserve the destination. The borrowed client remains open.
    """
    request_headers = httpx2.Headers(headers)
    request_headers.setdefault("Accept-Encoding", "identity")
    request = client.build_request("GET", url, headers=request_headers)
    response = await client.send(request, stream=True, follow_redirects=False)
    try:
        response.raise_for_status()
        count = 0

        async def receive() -> None:
            nonlocal count
            count = await write_file(local_path, response.aiter_raw(_CHUNK_SIZE))

        # Raw iteration closes the response itself. Translate direct Task.cancel()
        # into scope cancellation so httpcore's close shields remain effective,
        # while pending network reads are still canceled. The group joins cleanup.
        try:
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(receive)
        except BaseExceptionGroup as errors:
            # There is exactly one child; preserve the public file/HTTP exception.
            raise errors.exceptions[0] from None
        return count
    finally:
        await _finish_io(response.aclose())

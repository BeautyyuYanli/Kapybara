"""Real local servers own their listening sockets and ASGI lifespans."""

import asyncio
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
import uvicorn
from starlette.types import ASGIApp

from mcp_gateway import TokenCodec, decode_key, generate_key


@pytest.fixture
def codec() -> TokenCodec:
    return TokenCodec({"primary": decode_key(generate_key())}, active_key_id="primary")


@asynccontextmanager
async def serve(app: ASGIApp) -> AsyncIterator[str]:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                log_level="error",
                timeout_graceful_shutdown=3,
                ws="none",
            )
        )
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    if task.done():
                        task.result()
                        raise RuntimeError("Test server exited before starting")
                    await asyncio.sleep(0.01)
            yield f"http://127.0.0.1:{sock.getsockname()[1]}"
        finally:
            server.should_exit = True
            async with asyncio.timeout(10):
                await task

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from kapy.execution import SessionProxyAuth, call_local_proxy
from kapy.rpc import RpcDisconnected, RpcError, RpcTimeout


@pytest.mark.asyncio
async def test_local_proxy_envelope_preserves_origin_and_target() -> None:
    captured: list[dict] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            captured.append(request)
            response = json.dumps(
                {"jsonrpc": "2.0", "id": request["id"], "result": {"accepted": True}}
            )
            # Deliberately split a frame to exercise the incremental decoder.
            writer.write(response[:10].encode())
            await writer.drain()
            writer.write(response[10:].encode() + b"\n")
            await writer.drain()
            assert await reader.read() == b""
        finally:
            writer.close()
            await writer.wait_closed()

    with tempfile.TemporaryDirectory(prefix="kapy-proxy-") as directory:
        path = Path(directory) / "daemon.sock"
        async with await asyncio.start_unix_server(serve, path) as server:
            auth: SessionProxyAuth = {"kind": "session", "session_id": "origin", "token": "private"}
            result = await call_local_proxy(
                path,
                "session.input",
                {"session_id": "target", "payload": "line\nbreak"},
                auth=auth,
            )
            assert result == {"accepted": True}
            assert captured[0]["method"] == "proxy.call"
            assert captured[0]["params"] == {
                "auth": auth,
                "method": "session.input",
                "params": {"session_id": "target", "payload": "line\nbreak"},
            }
            server.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["error", "timeout", "oversized", "bad_utf8", "eof"])
async def test_local_proxy_failure_paths_close_connection(mode: str) -> None:
    closed = asyncio.Event()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request = json.loads(await reader.readline())
            if mode == "error":
                result = {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32001, "message": "Denied"},
                }
                writer.write(json.dumps(result).encode() + b"\n")
            elif mode == "oversized":
                writer.write(b"x" * 1_048_576)
            elif mode == "bad_utf8":
                writer.write(b"\xff\n")
            elif mode == "eof":
                return
            await writer.drain()
            await reader.read()
        except BrokenPipeError, ConnectionResetError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except BrokenPipeError, ConnectionResetError:
                pass
            closed.set()

    with tempfile.TemporaryDirectory(prefix="kapy-proxy-") as directory:
        path = Path(directory) / "daemon.sock"
        async with await asyncio.start_unix_server(serve, path):
            expected = (
                RpcError
                if mode == "error"
                else RpcTimeout
                if mode == "timeout"
                else RpcDisconnected
            )
            with pytest.raises(expected):
                await call_local_proxy(
                    path, "session.list", {}, auth={"kind": "user", "token": "secret"}, timeout=0.05
                )
            await asyncio.wait_for(closed.wait(), 1)


@pytest.mark.asyncio
async def test_missing_local_daemon_is_a_connection_error(tmp_path: Path) -> None:
    with pytest.raises(RpcDisconnected):
        await call_local_proxy(
            tmp_path / "absent", "session.list", {}, auth={"kind": "user", "token": "x"}
        )

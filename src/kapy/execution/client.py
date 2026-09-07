"""The CLI's single-call client and bounded local NDJSON transport."""

import asyncio
import math
import os
import socket
import struct
from pathlib import Path
from typing import cast

import anyio
from anyio.abc import SocketAttribute, UNIXSocketStream

from kapy.rpc import (
    JsonObject,
    JsonParams,
    JsonValue,
    RpcDisconnected,
    RpcError,
    RpcPeer,
    RpcTimeout,
)
from kapy.rpc.messages import MAX_MESSAGE_BYTES

from ._common import fields, invalid, session_identifier, string
from .types import ProxyAuth


def validate_auth(auth: JsonValue) -> ProxyAuth:
    if not isinstance(auth, dict):
        raise invalid("auth must be an object")
    if auth.get("kind") == "session":
        fields(auth, {"kind", "session_id", "token"})
        session_identifier(auth["session_id"])
    elif auth.get("kind") == "user":
        fields(auth, {"kind", "token"})
    else:
        raise invalid("auth kind must be session or user")
    string(auth["token"], "token")
    return cast(ProxyAuth, auth)


class LocalTransport:
    """Own a same-UID Unix stream with a 1 MiB limit including each LF."""

    def __init__(self, stream: UNIXSocketStream) -> None:
        self._stream = stream
        self._buffer = bytearray()
        raw_socket = stream.extra(SocketAttribute.raw_socket)
        credentials = raw_socket.getsockopt(
            socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
        )
        _, uid, _ = struct.unpack("3i", credentials)
        if uid != os.getuid():
            raise RpcDisconnected("Local proxy requires the same UID")

    async def send_text(self, payload: str) -> None:
        data = payload.encode("utf-8") + b"\n"
        if len(data) > MAX_MESSAGE_BYTES:
            raise RpcDisconnected("Local RPC frame exceeds its size limit")
        await self._stream.send(data)

    async def receive_text(self) -> str | None:
        while True:
            newline = self._buffer.find(b"\n")
            if newline >= 0:
                line = bytes(self._buffer[:newline])
                del self._buffer[: newline + 1]
                try:
                    return line.decode("utf-8", errors="strict")
                except UnicodeError as exc:
                    raise RpcDisconnected("Local RPC frame is not UTF-8") from exc
            remaining = MAX_MESSAGE_BYTES - len(self._buffer)
            if remaining == 0:
                raise RpcDisconnected("Local RPC frame exceeds its size limit")
            try:
                self._buffer.extend(await self._stream.receive(min(65_536, remaining)))
            except anyio.EndOfStream:
                if self._buffer:
                    raise RpcDisconnected("Local RPC frame ended without LF") from None
                return None

    async def aclose(self) -> None:
        self._buffer.clear()
        await self._stream.aclose()


async def call_local_proxy(
    socket_path: Path,
    method: str,
    params: JsonObject,
    *,
    auth: ProxyAuth,
    timeout: float = 60.0,  # noqa: ASYNC109 - approved CLI client contract
) -> JsonValue:
    """Call control through the local daemon without retrying uncertain writes.

    auth.session_id names the caller; params.session_id may name another target.
    This helper reads no environment or credentials and owns its one connection.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Proxy timeout must be positive and finite")
    if not isinstance(params, dict):
        raise invalid("params must be an object")
    string(method, "method")
    validate_auth(cast(JsonValue, auth))

    async def reject_request(method: str, params: JsonParams) -> JsonValue:
        raise RpcError(-32601, "Method not found")

    try:
        async with asyncio.timeout(timeout):
            stream = await anyio.connect_unix(socket_path)
            try:
                transport = LocalTransport(stream)
            except BaseException:
                await stream.aclose()
                raise
            async with RpcPeer(
                send_text=transport.send_text,
                receive_text=transport.receive_text,
                close_transport=transport.aclose,
                handler=reject_request,
            ) as peer:
                return await peer.call(
                    "proxy.call",
                    {"auth": cast(JsonValue, auth), "method": method, "params": params},
                    timeout=timeout,
                )
    except TimeoutError as exc:
        raise RpcTimeout("Local proxy deadline expired") from exc
    except (OSError, anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
        raise RpcDisconnected("Local proxy connection failed") from exc

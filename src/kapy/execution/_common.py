"""Small validation and bounded I/O helpers shared by execution managers."""

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import Callable, Set
from functools import partial
from typing import cast
from uuid import UUID

import anyio

from kapy.rpc import JsonObject, JsonValue, RpcError

CHUNK_SIZE = 65_536


def error(kind: str, message: str, **data: JsonValue) -> RpcError:
    codes = {
        "unauthorized": -32001,
        "not_found": -32004,
        "conflict": -32009,
        "gone": -32010,
        "resource_limit": -32020,
        "io_error": -32021,
        "offline": -32022,
    }
    return RpcError(codes[kind], message, {"kind": kind, **data})


def invalid(message: str = "Invalid params") -> RpcError:
    return RpcError(-32602, message)


def fields(params: JsonObject, required: Set[str], optional: Set[str] = frozenset()) -> None:
    if not required <= params.keys() or params.keys() - required - optional:
        raise invalid()


def string(value: JsonValue, name: str) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise invalid(f"{name} must be a nonempty string without NUL")
    return value


def integer(value: JsonValue, name: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise invalid(f"{name} is outside its supported range")
    return value


def identifier(value: JsonValue, name: str) -> str:
    try:
        return str(UUID(string(value, name)))
    except ValueError as exc:
        raise invalid(f"{name} must be a UUID") from exc


def session_identifier(value: JsonValue) -> str:
    session_id = string(value, "session_id")
    try:
        size = len(session_id.encode("utf-8"))
    except UnicodeError as exc:
        raise invalid("session_id must be valid UTF-8") from exc
    if size > 128:
        raise invalid("session_id exceeds 128 UTF-8 bytes")
    return session_id


def decode_chunk(value: JsonValue) -> bytes:
    if not isinstance(value, str) or len(value) > ((CHUNK_SIZE + 2) // 3) * 4:
        raise invalid("data_base64 exceeds the chunk limit")
    try:
        data = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise invalid("data_base64 is not valid base64") from exc
    if len(data) > CHUNK_SIZE:
        raise invalid("data_base64 exceeds the chunk limit")
    return data


def byte_chunk(
    data: bytes, start: int, available: int, *, eof: bool, truncated: bool = False
) -> JsonObject:
    return {
        "data_base64": base64.b64encode(data).decode("ascii"),
        "start": start,
        "next": start + len(data),
        "available": available,
        "truncated": truncated,
        "eof": eof,
    }


def fingerprint(value: JsonObject) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class IOWorker:
    """Limit blocking operations, and finish one before cancellation closes its FD."""

    def __init__(self) -> None:
        self._limiter = anyio.CapacityLimiter(4)

    async def run[T](self, operation: Callable[..., T], *args: object) -> T:
        call = cast(Callable[[], T], partial(operation, *args))
        task = asyncio.create_task(anyio.to_thread.run_sync(call, limiter=self._limiter))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

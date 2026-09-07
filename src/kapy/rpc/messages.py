"""The shared JSON-RPC codec used by HTTP dispatch and duplex peers."""

import asyncio
import json
import math
from dataclasses import dataclass
from typing import cast

from .types import JsonObject, JsonParams, JsonValue, RequestHandler, RpcError

MAX_MESSAGE_BYTES = 1_048_576
MAX_DEPTH = 64
MAX_BATCH = 16
MAX_PENDING = 64
MAX_HANDLERS = 64
MAX_SEND_QUEUE = 64

type RequestId = str | int | float | None


def resource_error() -> RpcError:
    return RpcError(-32020, "Resource limit", {"kind": "resource_limit"})


def error_response(request_id: RequestId, error: RpcError) -> JsonObject:
    detail: JsonObject = {"code": error.code, "message": error.message}
    if error.data is not None:
        detail["data"] = error.data
    return {"jsonrpc": "2.0", "id": request_id, "error": detail}


def _check_json(value: object, depth: int = 0) -> None:
    """Check without allocating a second tree or an unbounded encoded string."""
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite JSON number")
        return
    if isinstance(value, str):
        if len(value) > MAX_MESSAGE_BYTES:
            raise resource_error()
        value.encode("utf-8", errors="strict")
        return
    if isinstance(value, list | dict):
        if depth >= MAX_DEPTH:
            raise resource_error()
        if isinstance(value, dict):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings")
                _check_json(key, depth + 1)
                _check_json(item, depth + 1)
        else:
            for item in value:
                _check_json(item, depth + 1)
        return
    raise ValueError("Unsupported JSON value")


def encode_json(value: JsonValue) -> str:
    _check_json(value)
    parts: list[str] = []
    size = 0
    encoder = json.JSONEncoder(ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    for part in encoder.iterencode(value):
        size += len(part.encode("utf-8"))
        if size > MAX_MESSAGE_BYTES:
            raise resource_error()
        parts.append(part)
    return "".join(parts)


def _reject_constant(value: str) -> None:
    raise ValueError("Non-finite JSON number")


def decode_json(payload: str) -> tuple[list[JsonValue], bool]:
    try:
        if len(payload) > MAX_MESSAGE_BYTES or len(payload.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise resource_error()
        value = json.loads(payload, parse_constant=_reject_constant)
        _check_json(value)
    except (ValueError, RecursionError) as exc:
        raise RpcError(-32700, "Parse error") from exc
    if isinstance(value, list):
        if not value:
            raise RpcError(-32600, "Invalid Request")
        if len(value) > MAX_BATCH:
            raise resource_error()
        return cast(list[JsonValue], value), True
    return [cast(JsonValue, value)], False


def valid_id(value: JsonValue) -> bool:
    return value is None or (isinstance(value, str | int | float) and not isinstance(value, bool))


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    params: JsonParams
    request_id: RequestId
    notification: bool


def request_from(value: JsonValue) -> Request | JsonObject | None:
    if (
        not isinstance(value, dict)
        or value.get("jsonrpc") != "2.0"
        or not isinstance(value.get("method"), str)
        or ("id" in value and not valid_id(value["id"]))
        or "result" in value
        or "error" in value
    ):
        return error_response(None, RpcError(-32600, "Invalid Request"))
    request_id = cast(RequestId, value.get("id"))
    notification = "id" not in value
    params = value.get("params", {})
    if not isinstance(params, dict | list):
        # This is a recognizable notification, even though its parameters fail.
        if notification:
            return None
        return error_response(request_id, RpcError(-32602, "Invalid params"))
    return Request(cast(str, value["method"]), params, request_id, notification)


async def invoke(request: Request, handler: RequestHandler) -> JsonObject | None:
    try:
        result = await handler(request.method, request.params)
        response: JsonObject = {"jsonrpc": "2.0", "id": request.request_id, "result": result}
    except RpcError as exc:
        response = error_response(request.request_id, exc)
    except Exception:
        # Exception details may contain credentials, paths or provider responses.
        response = error_response(request.request_id, RpcError(-32603, "Internal error"))
    if request.notification:
        return None
    try:
        encode_json(response)
    except RpcError:
        return error_response(request.request_id, resource_error())
    except ValueError, TypeError:
        return error_response(request.request_id, RpcError(-32603, "Internal error"))
    return response


def encode_responses(responses: list[JsonObject], batch: bool) -> str | None:
    if not responses:
        return None
    value: JsonValue = cast(list[JsonValue], responses) if batch else responses[0]
    try:
        return encode_json(value)
    except RpcError:
        # Individual valid responses can exceed the limit when aggregated.
        errors = [
            error_response(cast(RequestId, item["id"]), resource_error()) for item in responses
        ]
        try:
            return encode_json(cast(list[JsonValue], errors) if batch else errors[0])
        except RpcError:
            # A near-frame-sized id cannot fit even an error envelope.
            return encode_json(error_response(None, resource_error()))


async def dispatch_json(payload: str, handler: RequestHandler) -> str | None:
    """Dispatch an HTTP JSON-RPC body using the peer's validation and limits.

    The caller owns HTTP authentication, UTF-8 decoding and connection limits.
    No response body is returned for valid notifications. Handler exceptions are
    sanitized; explicitly raised RpcError values are safe public business errors.
    """
    try:
        items, batch = decode_json(payload)
    except RpcError as exc:
        return encode_json(error_response(None, exc))

    async def dispatch(item: JsonValue) -> JsonObject | None:
        request = request_from(item)
        if request is None:
            return None
        if isinstance(request, dict):
            return request
        return await invoke(request, handler)

    results = await asyncio.gather(*(dispatch(item) for item in items))
    return encode_responses([item for item in results if item is not None], batch)

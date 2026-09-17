import asyncio
import json

import pytest

from kapy.rpc import JsonParams, JsonValue, RpcError, dispatch_json


async def echo(method: str, params: JsonParams) -> JsonValue:
    if method == "echo":
        return params
    if method == "business_error":
        raise RpcError(-32009, "Conflict", {"kind": "conflict"})
    if method == "crash":
        raise RuntimeError("private credential must not escape")
    raise RpcError(-32601, "Method not found")


@pytest.mark.asyncio
@pytest.mark.parametrize("request_id", [None, 0, 1.5, "0"])
async def test_request_id_types_and_named_or_positional_params(request_id: JsonValue) -> None:
    for params in ({"value": "你好"}, [1, "two"]):
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": request_id, "method": "echo", "params": params}
        )
        response = await dispatch_json(payload, echo)
        assert response is not None
        assert json.loads(response) == {"jsonrpc": "2.0", "id": request_id, "result": params}


@pytest.mark.asyncio
async def test_missing_params_normalized_and_null_id_is_not_notification() -> None:
    response = await dispatch_json('{"jsonrpc":"2.0","id":null,"method":"echo"}', echo)
    assert response is not None
    assert json.loads(response) == {"jsonrpc": "2.0", "id": None, "result": {}}


@pytest.mark.asyncio
async def test_notifications_do_not_reply_or_execute_invalid_params() -> None:
    calls: list[JsonParams] = []

    async def handler(method: str, params: JsonParams) -> JsonValue:
        calls.append(params)
        raise RpcError(-32601, "Unknown")

    payload = '[{"jsonrpc":"2.0","method":"x"},{"jsonrpc":"2.0","method":"x","params":7}]'
    assert await dispatch_json(payload, handler) is None
    assert calls == [{}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "code", "request_id"),
    [
        ("{", -32700, None),
        ('{"jsonrpc":"2.0","id":1,"method":"echo","params":[NaN]}', -32700, None),
        ('{"jsonrpc":"2.0","id":1,"method":"echo","params":[1e999]}', -32700, None),
        ("[]", -32600, None),
        ("1", -32600, None),
        ('{"jsonrpc":"1.0","id":1,"method":"echo"}', -32600, None),
        ('{"jsonrpc":"2.0","id":true,"method":"echo"}', -32600, None),
        ('{"jsonrpc":"2.0","id":[],"method":"echo"}', -32600, None),
        ('{"jsonrpc":"2.0","id":3,"result":1}', -32600, None),
        ('{"jsonrpc":"2.0","id":3,"method":"echo","params":1}', -32602, 3),
        ('{"jsonrpc":"2.0","id":3,"method":"absent"}', -32601, 3),
    ],
)
async def test_protocol_errors(payload: str, code: int, request_id: JsonValue) -> None:
    response = await dispatch_json(payload, echo)
    assert response is not None
    decoded = json.loads(response)
    assert decoded["id"] == request_id
    assert decoded["error"]["code"] == code


@pytest.mark.asyncio
async def test_mixed_batch_filters_notifications_and_preserves_error_data() -> None:
    payload = json.dumps(
        [
            {"jsonrpc": "2.0", "id": "ok", "method": "echo", "params": [4]},
            {"jsonrpc": "2.0", "method": "echo"},
            {"jsonrpc": "2.0", "id": "bad", "method": "business_error"},
            {"jsonrpc": "2.0", "id": "private", "method": "crash"},
            False,
        ]
    )
    response = await dispatch_json(payload, echo)
    assert response is not None
    items = json.loads(response)
    assert len(items) == 4
    assert items[0] == {"jsonrpc": "2.0", "id": "ok", "result": [4]}
    assert items[1]["error"]["data"] == {"kind": "conflict"}
    assert items[2]["error"] == {"code": -32603, "message": "Internal error"}
    assert "credential" not in response
    assert items[3]["error"]["code"] == -32600


@pytest.mark.asyncio
async def test_batch_handlers_run_concurrently_and_cancellation_propagates() -> None:
    started = asyncio.Event()
    gate = asyncio.Event()
    active = 0
    finished = 0

    async def handler(method: str, params: JsonParams) -> JsonValue:
        nonlocal active, finished
        active += 1
        if active == 2:
            started.set()
        try:
            await gate.wait()
        finally:
            finished += 1
        return None

    payload = '[{"jsonrpc":"2.0","id":1,"method":"x"},{"jsonrpc":"2.0","id":2,"method":"x"}]'
    task = asyncio.create_task(dispatch_json(payload, handler))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == 2


@pytest.mark.asyncio
async def test_message_batch_and_depth_limits() -> None:
    request = {"jsonrpc": "2.0", "id": 1, "method": "echo"}
    for payload in (
        json.dumps([request] * 17),
        json.dumps({**request, "params": {"text": "界" * 350_000}}, ensure_ascii=False),
        '{"jsonrpc":"2.0","id":1,"method":"echo","params":' + "[" * 65 + "0" + "]" * 65 + "}",
    ):
        response = await dispatch_json(payload, echo)
        assert response is not None
        assert json.loads(response)["error"]["code"] == -32020
        assert len(response.encode()) <= 1_048_576


@pytest.mark.asyncio
async def test_oversized_results_and_aggregate_batch_remain_bounded() -> None:
    async def large_result(method: str, params: JsonParams) -> JsonValue:
        return "x" * (2_000_000 if method == "large" else 600_000)

    for payload in (
        '{"jsonrpc":"2.0","id":1,"method":"large"}',
        '[{"jsonrpc":"2.0","id":1,"method":"small"},{"jsonrpc":"2.0","id":2,"method":"small"}]',
    ):
        response = await dispatch_json(payload, large_result)
        assert response is not None
        assert len(response.encode()) <= 1_048_576
        value = json.loads(response)
        items = value if isinstance(value, list) else [value]
        assert all(item["error"]["code"] == -32020 for item in items)
        assert [item["id"] for item in items] == list(range(1, len(items) + 1))


@pytest.mark.asyncio
async def test_nonfinite_handler_result_is_sanitized() -> None:
    async def bad_result(method: str, params: JsonParams) -> JsonValue:
        return float("nan")

    response = await dispatch_json('{"jsonrpc":"2.0","id":1,"method":"x"}', bad_result)
    assert response is not None
    assert json.loads(response)["error"]["code"] == -32603

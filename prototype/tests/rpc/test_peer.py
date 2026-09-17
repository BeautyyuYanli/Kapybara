import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from kapy.rpc import (
    JsonParams,
    JsonValue,
    RequestHandler,
    RpcDisconnected,
    RpcError,
    RpcPeer,
    RpcTimeout,
)


@dataclass
class Wire:
    incoming: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    outgoing: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    closes: int = 0

    async def send(self, payload: str) -> None:
        await self.outgoing.put(payload)

    async def receive(self) -> str | None:
        return await self.incoming.get()

    async def close(self) -> None:
        self.closes += 1

    async def sent(self) -> Any:
        payload = await asyncio.wait_for(self.outgoing.get(), 1)
        assert payload is not None
        return json.loads(payload)

    def peer(self, handler: RequestHandler) -> RpcPeer:
        return RpcPeer(
            send_text=self.send,
            receive_text=self.receive,
            close_transport=self.close,
            handler=handler,
        )


async def echo(method: str, params: JsonParams) -> JsonValue:
    return params


@pytest.mark.asyncio
async def test_duplex_reverse_call_does_not_block_reader() -> None:
    left = Wire()
    right = Wire()
    left.incoming = right.outgoing
    right.incoming = left.outgoing

    async def server_handler(method: str, params: JsonParams) -> JsonValue:
        return await server.call("reverse", {"original": params}, timeout=1)

    async with left.peer(echo) as client, right.peer(server_handler) as server:
        results = await asyncio.gather(*(client.call("forward", [i], timeout=1) for i in range(16)))
        assert results == [{"original": [i]} for i in range(16)]
    assert left.closes == right.closes == 1


@pytest.mark.asyncio
async def test_timeout_late_response_and_next_call() -> None:
    wire = Wire()
    async with wire.peer(echo) as peer:
        first = asyncio.create_task(peer.call("first", {}, timeout=0.01))
        request = await wire.sent()
        with pytest.raises(RpcTimeout):
            await first
        await wire.incoming.put(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": "late"})
        )
        second = asyncio.create_task(peer.call("second", {}, timeout=1))
        request = await wire.sent()
        await wire.incoming.put(
            json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": "now"})
        )
        assert await second == "now"


@pytest.mark.asyncio
async def test_timeout_does_not_cancel_remote_work() -> None:
    left = Wire()
    right = Wire()
    left.incoming = right.outgoing
    right.incoming = left.outgoing
    started = asyncio.Event()
    finish = asyncio.Event()
    done = asyncio.Event()

    async def handler(method: str, params: JsonParams) -> JsonValue:
        started.set()
        await finish.wait()
        done.set()
        return None

    async with left.peer(echo) as client, right.peer(handler):
        task = asyncio.create_task(client.call("work", {}, timeout=0.02))
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(RpcTimeout):
            await task
        finish.set()
        await asyncio.wait_for(done.wait(), 1)


@pytest.mark.asyncio
async def test_disconnect_fails_pending_and_cancels_handlers() -> None:
    wire = Wire()
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(method: str, params: JsonParams) -> JsonValue:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with wire.peer(handler) as peer:
        pending = asyncio.create_task(peer.call("pending", {}, timeout=10))
        await wire.sent()
        await wire.incoming.put('{"jsonrpc":"2.0","id":4,"method":"wait"}')
        await asyncio.wait_for(started.wait(), 1)
        await wire.incoming.put(None)
        with pytest.raises(RpcDisconnected):
            await asyncio.wait_for(pending, 1)
        await asyncio.wait_for(peer.wait_closed(), 1)
        assert cancelled.is_set()
    assert wire.closes == 1


@pytest.mark.asyncio
async def test_business_error_and_notification_batch() -> None:
    wire = Wire()
    async with wire.peer(echo) as peer:
        task = asyncio.create_task(peer.call("error", {}, timeout=1))
        request = await wire.sent()
        await wire.incoming.put(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32009, "message": "Conflict", "data": {"kind": "conflict"}},
                }
            )
        )
        with pytest.raises(RpcError) as caught:
            await task
        assert caught.value.code == -32009
        assert caught.value.data == {"kind": "conflict"}
        await wire.incoming.put(
            '[{"jsonrpc":"2.0","method":"note"},{"jsonrpc":"2.0","id":null,"method":"echo"}]'
        )
        assert await wire.sent() == [{"jsonrpc": "2.0", "id": None, "result": {}}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        '{"jsonrpc":"2.0","id":"r1","result":1,"error":{}}',
        '{"jsonrpc":"2.0","id":"r1","error":{"code":true,"message":"bad"}}',
        '{"jsonrpc":"2.0","id":"r1"}',
        '{"jsonrpc":"2.0","id":[],"result":1}',
    ],
)
async def test_malformed_response_closes_and_fails_calls(response: str) -> None:
    wire = Wire()
    async with wire.peer(echo) as peer:
        pending = asyncio.create_task(peer.call("x", {}, timeout=1))
        await wire.sent()
        await wire.incoming.put(response)
        with pytest.raises(RpcDisconnected):
            await asyncio.wait_for(pending, 1)


@pytest.mark.asyncio
async def test_invalid_request_id_gets_error_without_closing_peer() -> None:
    wire = Wire()
    async with wire.peer(echo):
        await wire.incoming.put('{"jsonrpc":"2.0","id":{},"method":"echo"}')
        assert (await wire.sent())["error"]["code"] == -32600
        await wire.incoming.put('{"jsonrpc":"2.0","id":3,"method":"echo"}')
        assert (await wire.sent())["result"] == {}


@pytest.mark.asyncio
async def test_handler_capacity_rejects_work_while_responses_still_flow() -> None:
    wire = Wire()
    all_started = asyncio.Event()
    gate = asyncio.Event()
    active = 0

    async def handler(method: str, params: JsonParams) -> JsonValue:
        nonlocal active
        active += 1
        if active == 64:
            all_started.set()
        await gate.wait()
        return None

    async with wire.peer(handler) as peer:
        for request_id in range(64):
            await wire.incoming.put(
                json.dumps({"jsonrpc": "2.0", "id": request_id, "method": "hold"})
            )
        await asyncio.wait_for(all_started.wait(), 1)
        await wire.incoming.put('{"jsonrpc":"2.0","id":"over","method":"hold"}')
        assert (await wire.sent())["error"]["code"] == -32020
        pending = asyncio.create_task(peer.call("reverse", {}, timeout=1))
        request = await wire.sent()
        await wire.incoming.put(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": 5}))
        assert await pending == 5
        assert active == 64


@pytest.mark.asyncio
async def test_pending_limit_and_cancelled_calls_release_slots() -> None:
    wire = Wire()
    async with wire.peer(echo) as peer:
        pending = [asyncio.create_task(peer.call("hold", {}, timeout=10)) for _ in range(64)]
        for _ in range(64):
            await wire.sent()
        with pytest.raises(RpcError) as caught:
            await peer.call("overflow", {}, timeout=1)
        assert caught.value.code == -32020
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        next_call = asyncio.create_task(peer.call("next", {}, timeout=1))
        request = await wire.sent()
        await wire.incoming.put(json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": 1}))
        assert await next_call == 1


@pytest.mark.asyncio
async def test_send_queue_overflow_closes_instead_of_growing() -> None:
    wire = Wire()
    writing = asyncio.Event()

    async def blocked_send(payload: str) -> None:
        writing.set()
        await asyncio.Event().wait()

    async with RpcPeer(
        send_text=blocked_send,
        receive_text=wire.receive,
        close_transport=wire.close,
        handler=echo,
    ) as peer:
        await peer.notify("start", {})
        await asyncio.wait_for(writing.wait(), 1)
        for _ in range(64):
            await peer.notify("fill", {})
        with pytest.raises(RpcDisconnected):
            await peer.notify("overflow", {})
        await asyncio.wait_for(peer.wait_closed(), 1)
    assert wire.closes == 1


@pytest.mark.asyncio
async def test_peer_single_use_and_close_idempotence() -> None:
    wire = Wire()
    peer = wire.peer(echo)
    with pytest.raises(RuntimeError):
        await peer.call("early", {})
    async with peer:
        with pytest.raises(RuntimeError):
            await peer.__aenter__()
        await asyncio.gather(peer.aclose(), peer.aclose())
        with pytest.raises(RpcDisconnected):
            await peer.notify("late", {})
    with pytest.raises(RuntimeError):
        await peer.__aenter__()
    assert wire.closes == 1


@pytest.mark.asyncio
async def test_handler_can_close_its_peer_without_deadlock() -> None:
    wire = Wire()

    async def handler(method: str, params: JsonParams) -> JsonValue:
        await peer.aclose()
        return None

    async with wire.peer(handler) as peer:
        await wire.incoming.put('{"jsonrpc":"2.0","id":1,"method":"close"}')
        await asyncio.wait_for(peer.wait_closed(), 1)
    assert wire.closes == 1

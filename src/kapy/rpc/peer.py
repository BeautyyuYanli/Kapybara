"""A bounded, asynchronous JSON-RPC peer over an owned text transport."""

import asyncio
import logging
import math
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self, cast

from .messages import (
    MAX_HANDLERS,
    MAX_PENDING,
    MAX_SEND_QUEUE,
    Request,
    decode_json,
    encode_json,
    encode_responses,
    error_response,
    invoke,
    request_from,
    resource_error,
    valid_id,
)
from .types import (
    CloseTransport,
    JsonObject,
    JsonParams,
    JsonValue,
    ReceiveText,
    RequestHandler,
    RpcDisconnected,
    RpcError,
    RpcTimeout,
    SendText,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ResponseGroup:
    batch: bool
    responses: dict[int, JsonObject] = field(default_factory=dict)
    remaining: int = 0


class RpcPeer:
    """Own one connection, dispatching handlers without blocking its reader.

    Enter exactly once and keep the context open while using call/notify. The
    transport callbacks exchange whole text messages; EOF is receive_text=None.
    A connection close fails pending calls and cancels connection-level handlers.
    Long-running domain work must belong to a separate daemon/service task.
    """

    def __init__(
        self,
        *,
        send_text: SendText,
        receive_text: ReceiveText,
        close_transport: CloseTransport,
        handler: RequestHandler,
    ) -> None:
        self._send_text = send_text
        self._receive_text = receive_text
        self._close_transport = close_transport
        self._handler = handler
        self._outgoing: asyncio.Queue[str] = asyncio.Queue(MAX_SEND_QUEUE)
        self._pending: dict[str, asyncio.Future[JsonValue]] = {}
        self._handlers: set[asyncio.Task[None]] = set()
        self._reader: asyncio.Task[None] | None = None
        self._writer: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._closed = asyncio.Event()
        self._started = False
        self._closing = False
        self._next_id = 0
        self._dropped_notifications = 0

    async def __aenter__(self) -> Self:
        if self._started or self._closing:
            raise RuntimeError("RpcPeer is single-use")
        self._started = True
        self._writer = asyncio.create_task(self._write_loop(), name="rpc-writer")
        self._reader = asyncio.create_task(self._read_loop(), name="rpc-reader")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _require_open(self) -> None:
        if not self._started:
            raise RuntimeError("Enter RpcPeer before using it")
        if self._closing:
            raise RpcDisconnected("RPC connection is closed")

    def _enqueue(self, payload: str) -> None:
        self._require_open()
        try:
            self._outgoing.put_nowait(payload)
        except asyncio.QueueFull as exc:
            self._begin_close()
            raise RpcDisconnected("RPC send queue is full") from exc

    @staticmethod
    def _validate_call(method: str, params: JsonParams) -> None:
        if not isinstance(method, str) or not isinstance(params, dict | list):
            raise ValueError("RPC method must be a string and params an object or array")

    async def call(
        self,
        method: str,
        params: JsonParams,
        *,
        timeout: float = 60.0,  # noqa: ASYNC109
    ) -> JsonValue:
        self._require_open()
        self._validate_call(method, params)
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("RPC timeout must be positive and finite")
        if len(self._pending) >= MAX_PENDING:
            raise resource_error()
        self._next_id += 1
        request_id = f"r{self._next_id}"
        payload = encode_json(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        future: asyncio.Future[JsonValue] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._enqueue(payload)
            async with asyncio.timeout(timeout):
                return await future
        except TimeoutError as exc:
            raise RpcTimeout("RPC call deadline expired") from exc
        finally:
            self._pending.pop(request_id, None)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                # Also retrieve errors when queue failure happened before await.
                future.exception()

    async def notify(self, method: str, params: JsonParams) -> None:
        self._require_open()
        self._validate_call(method, params)
        self._enqueue(encode_json({"jsonrpc": "2.0", "method": method, "params": params}))

    async def wait_closed(self) -> None:
        if not self._started:
            raise RuntimeError("Enter RpcPeer before waiting for closure")
        await self._closed.wait()

    async def aclose(self) -> None:
        """Close the owned transport once, and finish all connection tasks."""
        task = self._begin_close()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # A handler may close its own peer; shutdown is cancelling it and
            # must not wait for a handler that in turn waits for shutdown.
            if asyncio.current_task() not in self._handlers:
                await asyncio.shield(task)
            raise

    def _begin_close(self) -> asyncio.Task[None]:
        if self._close_task is None:
            self._closing = True
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RpcDisconnected("RPC connection closed during call"))
            self._close_task = asyncio.create_task(self._shutdown(), name="rpc-close")
        return self._close_task

    async def _shutdown(self) -> None:
        tasks = [task for task in (self._reader, self._writer, *self._handlers) if task is not None]
        for task in tasks:
            task.cancel()
        try:
            await self._close_transport()
        except Exception:
            logger.warning("RPC transport close failed")
        finally:
            await asyncio.gather(*tasks, return_exceptions=True)
            while not self._outgoing.empty():
                self._outgoing.get_nowait()
            if self._dropped_notifications:
                logger.info(
                    "RPC discarded %d overloaded notifications", self._dropped_notifications
                )
            self._closed.set()

    async def _write_loop(self) -> None:
        try:
            while True:
                await self._send_text(await self._outgoing.get())
        except asyncio.CancelledError:
            raise
        except Exception:
            self._begin_close()

    async def _read_loop(self) -> None:
        try:
            while True:
                payload = await self._receive_text()
                if payload is None:
                    self._begin_close()
                    return
                self._receive_payload(payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Malformed responses and transport failures cannot resolve calls.
            self._begin_close()

    def _receive_payload(self, payload: str) -> None:
        try:
            items, batch = decode_json(payload)
        except RpcError as exc:
            self._enqueue(encode_json(error_response(None, exc)))
            return
        group = _ResponseGroup(batch=batch)
        for index, item in enumerate(items):
            if (
                isinstance(item, dict)
                and "method" not in item
                and (
                    "result" in item
                    or "error" in item
                    or (isinstance(item.get("id"), str) and item.get("id") in self._pending)
                )
            ):
                self._receive_response(item)
                continue
            request = request_from(item)
            if request is None:
                continue
            if isinstance(request, dict):
                group.responses[index] = request
            elif len(self._handlers) >= MAX_HANDLERS:
                if request.notification:
                    self._dropped_notifications += 1
                else:
                    group.responses[index] = error_response(request.request_id, resource_error())
            else:
                group.remaining += 1
                task = asyncio.create_task(
                    self._run_handler(request, group, index), name="rpc-handler"
                )
                self._handlers.add(task)
                task.add_done_callback(self._handlers.discard)
        if group.remaining == 0:
            self._finish_group(group)

    def _receive_response(self, value: JsonObject) -> None:
        if (
            value.get("jsonrpc") != "2.0"
            or "id" not in value
            or not valid_id(value["id"])
            or (("result" in value) == ("error" in value))
        ):
            raise RpcDisconnected("Malformed RPC response")
        error: RpcError | None = None
        if "error" in value:
            detail = value["error"]
            if (
                not isinstance(detail, dict)
                or not isinstance(detail.get("code"), int)
                or isinstance(detail.get("code"), bool)
                or not isinstance(detail.get("message"), str)
            ):
                raise RpcDisconnected("Malformed RPC error")
            error = RpcError(
                cast(int, detail["code"]), cast(str, detail["message"]), detail.get("data")
            )
        request_id = value["id"]
        # Our IDs are strings. Numeric/null and late IDs cannot match a call.
        future = self._pending.get(request_id) if isinstance(request_id, str) else None
        if future is not None and not future.done():
            if error is not None:
                future.set_exception(error)
            else:
                future.set_result(value["result"])

    async def _run_handler(self, request: Request, group: _ResponseGroup, index: int) -> None:
        result = await invoke(request, self._handler)
        if result is not None:
            group.responses[index] = result
        group.remaining -= 1
        if group.remaining == 0 and not self._closing:
            try:
                self._finish_group(group)
            except RpcDisconnected:
                pass  # Queue overflow has already started connection shutdown.

    def _finish_group(self, group: _ResponseGroup) -> None:
        payload = encode_responses(
            [group.responses[key] for key in sorted(group.responses)], group.batch
        )
        if payload is not None:
            self._enqueue(payload)

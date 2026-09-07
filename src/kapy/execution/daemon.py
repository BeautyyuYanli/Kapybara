"""Machine service assembly and reconnecting outbound JSON-RPC transport."""

import asyncio
import random
import time
from contextlib import AsyncExitStack
from typing import cast

import anyio
import httpx2
from anyio.abc import SocketStream, UNIXSocketStream
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus
from websockets.typing import Subprotocol

from kapy.rpc import JsonObject, JsonParams, JsonValue, RpcDisconnected, RpcError, RpcPeer
from kapy.rpc.messages import MAX_MESSAGE_BYTES

from ._common import error, fields, integer, invalid, session_identifier, string
from .client import LocalTransport, validate_auth
from .config import DaemonConfig
from .files import FileManager
from .paths import resolve_paths
from .processes import ProcessManager
from .store import ExecutionStore

MAX_RELEASES = 64


class MachineService:
    """Actual file/process/session handler, also usable by container integrations.

    The caller enters ExecutionStore and HTTP client first, then initialize(),
    and always aclose() before closing the store. Domain operations outlive peers.
    """

    def __init__(
        self,
        store: ExecutionStore,
        *,
        http_client: httpx2.AsyncClient,
        child_env: dict[str, str] | None = None,
    ) -> None:
        self.store = store
        self.files = FileManager(store, http_client=http_client)
        self.processes = ProcessManager(store, child_env=child_env)
        self._operations: set[asyncio.Task[JsonValue]] = set()
        self._releases: dict[str, asyncio.Task[None]] = {}
        self._session_lock = asyncio.Lock()
        self._closing = False
        self.last_activity = time.monotonic()

    @property
    def busy(self) -> bool:
        return bool(
            self._operations
            or self._releases
            or self.files.active_count
            or self.processes.active_count
        )

    async def initialize(self) -> None:
        await self.processes.initialize()
        await self.files.initialize()
        for sid in await self.store.releasing_sessions():
            await self._release(sid)

    async def handle(self, method: str, params: JsonParams) -> JsonValue:
        if self._closing:
            raise error("offline", "Machine service is closing")
        if not isinstance(params, dict):
            raise invalid()
        if len(self._operations) >= 64:
            raise error("resource_limit", "Too many machine operations")
        self.last_activity = time.monotonic()
        task = asyncio.create_task(self._dispatch(method, params), name="machine-operation")
        self._operations.add(task)

        def finished(done: asyncio.Task[JsonValue]) -> None:
            self._operations.discard(done)
            self.last_activity = time.monotonic()
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _dispatch(self, method: str, params: JsonObject) -> JsonValue:
        if method == "session.ensure":
            fields(params, {"session_id", "session_token"})
            sid = session_identifier(params["session_id"])
            token = string(params["session_token"], "session_token")
            async with self._session_lock:
                return await self.store.ensure_session(sid, token)
        if method == "session.release":
            fields(params, {"session_id"}, {"wait_ms"})
            sid = session_identifier(params["session_id"])
            wait_ms = integer(params.get("wait_ms", 5000), "wait_ms", maximum=30_000)
            async with self._session_lock:
                task = self._releases.get(sid)
                if task is None:
                    if len(self._releases) >= MAX_RELEASES:
                        raise error("resource_limit", "Too many session releases")
                    if not await self.store.begin_release(sid):
                        return {"session_id": sid, "released": True}
                    task = asyncio.create_task(self._release(sid), name="session-release")
                    self._releases[sid] = task

                    def finished(done: asyncio.Task[None]) -> None:
                        self._releases.pop(sid, None)
                        if not done.cancelled():
                            done.exception()

                    task.add_done_callback(finished)
            if wait_ms:
                try:
                    async with asyncio.timeout(wait_ms / 1000):
                        await asyncio.shield(task)
                except TimeoutError:
                    pass
            if task.done():
                task.result()
            return {"session_id": sid, "released": task.done()}
        if method.startswith("process."):
            return await self.processes.handle(method, params)
        if method.startswith("file."):
            return await self.files.handle(method, params)
        raise RpcError(-32601, "Method not found")

    async def _release(self, sid: str) -> None:
        await self.processes.release_session(sid)
        await self.files.abort_session(sid)
        await self.store.finish_release(sid)

    async def aclose(self) -> None:
        self._closing = True
        operations = list(self._operations)
        for task in operations:
            task.cancel()
        await asyncio.gather(*operations, return_exceptions=True)
        await self.processes.aclose()
        await self.files.aclose()
        await asyncio.gather(*self._releases.values(), return_exceptions=True)


class _Connection:
    def __init__(self, config: DaemonConfig, service: MachineService) -> None:
        self.config = config
        self.service = service
        self.peer: RpcPeer | None = None
        self.available = asyncio.Event()
        self.wake = asyncio.Event()
        self.local_count = 0
        self.local_peers: set[RpcPeer] = set()

    async def local(self, stream: SocketStream) -> None:
        if self.local_count >= 32:
            await stream.aclose()
            return
        self.local_count += 1
        try:
            transport = LocalTransport(cast(UNIXSocketStream, stream))
            async with RpcPeer(
                send_text=transport.send_text,
                receive_text=transport.receive_text,
                close_transport=transport.aclose,
                handler=self.proxy,
            ) as peer:
                self.local_peers.add(peer)
                try:
                    await peer.wait_closed()
                finally:
                    self.local_peers.discard(peer)
        finally:
            self.local_count -= 1
            await stream.aclose()

    async def proxy(self, method: str, params: JsonParams) -> JsonValue:
        if method != "proxy.call":
            raise RpcError(-32601, "Method not found")
        if not isinstance(params, dict):
            raise invalid()
        fields(params, {"auth", "method", "params"})
        auth = validate_auth(params["auth"])
        string(params["method"], "method")
        if not isinstance(params["params"], dict):
            raise invalid("params must be an object")
        if auth["kind"] == "session" and not self.service.store.authenticate_session(
            auth["session_id"], auth["token"]
        ):
            raise error("unauthorized", "Invalid caller session credentials")
        self.service.last_activity = time.monotonic()
        self.wake.set()
        try:
            async with asyncio.timeout(60):
                await self.available.wait()
                peer = self.peer
                if peer is None:
                    raise error("offline", "Machine is disconnected")
                return await peer.call("control.proxy", params)
        except (TimeoutError, RpcDisconnected) as exc:
            raise error("offline", "Control proxy unavailable; request was not retried") from exc
        finally:
            self.service.last_activity = time.monotonic()

    async def run(self) -> None:
        delay = 1.0
        while True:
            self.wake.clear()
            idle = False
            connected_at = time.monotonic()
            try:
                async with connect(
                    self.config.gateway_url,
                    subprotocols=[Subprotocol("kapy.jsonrpc.v1")],
                    additional_headers={
                        "Authorization": "Bearer " + self.config.machine_token.get_secret_value()
                    },
                    proxy=None,
                    max_size=MAX_MESSAGE_BYTES,
                    max_queue=16,
                    open_timeout=10,
                    close_timeout=2,
                ) as ws:
                    if ws.subprotocol != "kapy.jsonrpc.v1":
                        raise RuntimeError("Gateway did not select kapy.jsonrpc.v1")
                    fatal_protocol_error = False

                    async def receive() -> str | None:
                        nonlocal fatal_protocol_error
                        try:
                            message = await ws.recv()
                        except ConnectionClosed:
                            return None
                        if not isinstance(message, str):
                            fatal_protocol_error = True
                            raise RpcDisconnected("Machine RPC requires text frames")
                        return message

                    async with RpcPeer(
                        send_text=ws.send,
                        receive_text=receive,
                        close_transport=ws.close,
                        handler=self.service.handle,
                    ) as peer:
                        self.peer = peer
                        self.service.last_activity = time.monotonic()
                        self.available.set()
                        closed = asyncio.create_task(peer.wait_closed())
                        try:
                            while not closed.done():
                                threshold = self.config.idle_disconnect_after_s
                                if (
                                    threshold is not None
                                    and not self.service.busy
                                    and not self.local_count
                                    and time.monotonic() - self.service.last_activity >= threshold
                                ):
                                    idle = True
                                    break
                                await asyncio.wait({closed}, timeout=0.1)
                        finally:
                            self.available.clear()
                            self.peer = None
                            closed.cancel()
                            await asyncio.gather(closed, return_exceptions=True)
                    if fatal_protocol_error:
                        raise RuntimeError("Machine RPC requires text frames")
            except InvalidStatus as exc:
                if exc.response.status_code in {400, 401, 403, 404, 426}:
                    raise RuntimeError(
                        "Gateway rejected machine authentication or endpoint"
                    ) from None
            except InvalidHandshake:
                raise RuntimeError("Gateway WebSocket protocol negotiation failed") from None
            except OSError, TimeoutError:
                pass
            finally:
                self.available.clear()
                self.peer = None
            if time.monotonic() - connected_at > 30:
                delay = 1.0
            sleep_for = (
                self.config.idle_reconnect_after_s
                if idle
                else min(30.0, max(1.0, delay * random.uniform(0.8, 1.2)))
            )
            if not idle:
                delay = min(30, delay * 2)
            try:
                async with asyncio.timeout(sleep_for):
                    await self.wake.wait()
            except TimeoutError:
                pass


async def run_daemon(config: DaemonConfig, *, stop: anyio.Event | None = None) -> None:
    """Own the durable machine service until stop, cancellation, or fatal WS error."""
    paths = resolve_paths(
        state_dir=config.state_dir, data_dir=config.data_dir, runtime_dir=config.runtime_dir
    )
    stack = AsyncExitStack()
    try:
        # Registration must be atomic with acquisition: cancellation between
        # _open taking locks and __aenter__ returning otherwise leaves no exit
        # callback for the outer shielded cleanup to invoke.
        with anyio.CancelScope(shield=True):
            store = await stack.enter_async_context(ExecutionStore(paths, config.machine_id))
        http = await stack.enter_async_context(
            httpx2.AsyncClient(trust_env=False, follow_redirects=False)
        )
        service = MachineService(store, http_client=http, child_env=config.child_env)
        stack.push_async_callback(service.aclose)
        await service.initialize()
        # Exclusive runtime lock already held. An old daemon socket is now stale.
        await store.io.run(paths.socket_path.unlink, True)
        listener = await anyio.create_unix_listener(paths.socket_path, mode=0o600)
        await stack.enter_async_context(listener)
        connection = _Connection(config, service)
        tasks = [
            asyncio.create_task(listener.serve(connection.local), name="local-proxy"),
            asyncio.create_task(connection.run(), name="machine-websocket"),
        ]
        stop_task = asyncio.create_task(stop.wait()) if stop is not None else None
        try:
            watched = [*tasks, *([stop_task] if stop_task is not None else [])]
            done, _ = await asyncio.wait(watched, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        finally:
            with anyio.CancelScope(shield=True):
                for task in [*tasks, *([stop_task] if stop_task is not None else [])]:
                    task.cancel()
                await asyncio.gather(
                    *tasks, *([stop_task] if stop_task is not None else []), return_exceptions=True
                )
                await asyncio.gather(
                    *(peer.aclose() for peer in connection.local_peers), return_exceptions=True
                )
                await store.io.run(paths.socket_path.unlink, True)
    finally:
        # Shield the entire exit stack as well as connection teardown: AnyIO
        # cancellation is level-triggered and otherwise interrupts each I/O.
        with anyio.CancelScope(shield=True):
            await stack.aclose()

"""Full daemon transport and lifecycle tests inside the isolated machine image."""

import asyncio
import base64
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import anyio
import httpx2
import pytest
from pydantic import SecretStr, ValidationError
from websockets.asyncio.server import serve
from websockets.typing import Subprotocol

from kapy.execution import DaemonConfig, call_local_proxy, run_daemon
from kapy.execution.daemon import MachineService
from kapy.execution.paths import resolve_paths
from kapy.execution.store import ExecutionStore
from kapy.rpc import JsonObject, RpcError, RpcPeer


def config(tmp_path: Path, url: str, **kwargs: Any) -> DaemonConfig:
    return DaemonConfig(
        machine_id="machine",
        gateway_url=url,
        machine_token=SecretStr("machine-only"),
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "run",
        **kwargs,
    )


@pytest.mark.parametrize(
    "override",
    [
        {"state_dir": Path("relative")},
        {"gateway_url": "ws://remote/rpc/machines/m"},
        {"idle_reconnect_after_s": float("inf")},
        {"idle_disconnect_after_s": 0},
        {"cgroup_root": "/sys"},
        {"machine_token": SecretStr("x\r\ny")},
    ],
)
def test_invalid_config(override):
    with pytest.raises(ValidationError):
        DaemonConfig.model_validate(
            {
                "machine_id": "m",
                "gateway_url": "ws://127.0.0.1/rpc/machines/m",
                "machine_token": SecretStr("token"),
                **override,
            }
        )


@pytest.mark.asyncio
async def test_real_daemon_ws_proxy_reconnect_and_stop(tmp_path: Path):
    peers: asyncio.Queue[RpcPeer] = asyncio.Queue()
    calls: list[Any] = []

    async def gateway(ws):
        assert ws.request.headers["Authorization"] == "Bearer machine-only"
        assert ws.request.path == "/rpc/machines/machine"

        async def handler(method, params):
            calls.append((method, params))
            return {"target": params["params"]["session_id"]}

        async def receive():
            try:
                return await ws.recv()
            except Exception:
                return None

        async with RpcPeer(
            send_text=ws.send, receive_text=receive, close_transport=ws.close, handler=handler
        ) as peer:
            await peers.put(peer)
            await peer.wait_closed()

    async with serve(
        gateway, "127.0.0.1", 0, subprotocols=[Subprotocol("kapy.jsonrpc.v1")]
    ) as server:
        port = server.sockets[0].getsockname()[1]
        settings = config(tmp_path, f"ws://127.0.0.1:{port}/rpc/machines/machine")
        stop = anyio.Event()
        daemon = asyncio.create_task(run_daemon(settings, stop=stop))
        try:
            async with asyncio.timeout(10):
                peer = await peers.get()
                await peer.call(
                    "session.ensure", {"session_id": "caller", "session_token": "caller-token"}
                )
                response = await call_local_proxy(
                    tmp_path / "run" / "daemon.sock",
                    "session.read",
                    {"session_id": "different-target"},
                    auth={"kind": "session", "session_id": "caller", "token": "caller-token"},
                )
                assert response == {"target": "different-target"}
                assert calls[0][0] == "control.proxy"
                with pytest.raises(RpcError, match="Invalid caller"):
                    await call_local_proxy(
                        tmp_path / "run" / "daemon.sock",
                        "session.read",
                        {},
                        auth={"kind": "session", "session_id": "caller", "token": "bad"},
                    )
                pid = str(uuid4())
                params: JsonObject = {
                    "session_id": "caller",
                    "process_id": pid,
                    "mode": "stdio",
                    "argv": [
                        sys.executable,
                        "-c",
                        "import time; time.sleep(.5); print('survived')",
                    ],
                    "wait_ms": 0,
                }
                await peer.call("process.start", params)
                await peer.aclose()
                new_peer = await peers.get()
                await new_peer.call(
                    "session.ensure", {"session_id": "caller", "session_token": "fresh"}
                )
                result = cast(
                    Any,
                    await new_peer.call(
                        "process.wait", {"session_id": "caller", "process_id": pid, "wait_ms": 3000}
                    ),
                )
                assert base64.b64decode(result["output"]["stdout"]["data_base64"]) == b"survived\n"
                assert result["process"]["output_complete"]
                assert await new_peer.call("session.release", {"session_id": "caller"}) == {
                    "session_id": "caller",
                    "released": True,
                }
        finally:
            stop.set()
            await asyncio.wait_for(daemon, 10)
        assert not (tmp_path / "run" / "daemon.sock").exists()


@pytest.mark.asyncio
async def test_recovery_lost_and_finished_output(tmp_path: Path):
    paths = resolve_paths(
        state_dir=tmp_path / "state", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run"
    )
    pid = str(uuid4())
    params: JsonObject = {
        "session_id": "s",
        "process_id": pid,
        "mode": "stdio",
        "argv": ["/bin/echo", "kept"],
    }
    async with ExecutionStore(paths, "m") as store, httpx2.AsyncClient(trust_env=False) as http:
        service = MachineService(store, http_client=http)
        await service.initialize()
        await store.ensure_session("s", "secret")
        await service.handle("process.start", params)
        await service.aclose()
        records = await store.process_records()
        record = records[0]
        info = cast(Any, record["info"])
        meta = cast(Any, record["meta"])
        info["process_id"] = str(uuid4())
        info["state"] = "running"
        meta["pid"] = 2147483647
        await store.save_process("interrupted", info, meta)
    async with ExecutionStore(paths, "m") as store, httpx2.AsyncClient(trust_env=False) as http:
        service = MachineService(store, http_client=http)
        await service.initialize()
        try:
            result = cast(
                Any,
                await service.handle(
                    "process.wait", {"session_id": "s", "process_id": pid, "wait_ms": 0}
                ),
            )
            assert base64.b64decode(result["output"]["stdout"]["data_base64"]) == b"kept\n"
            listed = cast(Any, await service.handle("process.list", {"session_id": "s"}))
            assert {item["state"] for item in listed["items"]} == {"exited", "lost"}
            with pytest.raises(RpcError, match="ensured"):
                await service.handle("process.start", params)
        finally:
            await service.aclose()


@pytest.mark.asyncio
async def test_idle_reconnect_and_local_wake(tmp_path: Path):
    connected: asyncio.Queue[RpcPeer] = asyncio.Queue()

    async def gateway(ws):
        async def handle(method, params):
            return "awake"

        async def receive():
            try:
                return await ws.recv()
            except Exception:
                return None

        async with RpcPeer(
            send_text=ws.send, receive_text=receive, close_transport=ws.close, handler=handle
        ) as peer:
            await connected.put(peer)
            await peer.wait_closed()

    async with serve(
        gateway, "127.0.0.1", 0, subprotocols=[Subprotocol("kapy.jsonrpc.v1")]
    ) as server:
        port = server.sockets[0].getsockname()[1]
        settings = config(
            tmp_path,
            f"ws://127.0.0.1:{port}/rpc/machines/machine",
            idle_disconnect_after_s=0.1,
            idle_reconnect_after_s=0.3,
        )
        stop = anyio.Event()
        daemon = asyncio.create_task(run_daemon(settings, stop=stop))
        try:
            async with asyncio.timeout(5):
                first = await connected.get()
                await first.wait_closed()
                second = await connected.get()
                await second.wait_closed()
                response = await call_local_proxy(
                    tmp_path / "run" / "daemon.sock",
                    "session.list",
                    {},
                    auth={"kind": "user", "token": "user-token"},
                )
                assert response == "awake"
        finally:
            stop.set()
            await asyncio.wait_for(daemon, 5)

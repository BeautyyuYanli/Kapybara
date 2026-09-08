"""Real composition: run explicitly inside kapy-v2-machine:dev, never on the host."""

import asyncio
import base64
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx2
import psycopg
import pytest
import uvicorn
from psycopg import sql
from valkey.asyncio import Valkey

from kapy.execution import call_local_proxy, resolve_paths
from kapy.gateway import create_app
from kapy.settings import Settings

from .conftest import DATABASE, VALKEY, register_model

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("KAPY_DOCKER_TEST") != "1" or not Path("/.dockerenv").exists(),
        reason="Real execution is permitted only in the explicit machine-container run",
    ),
]


async def test_cli_daemon_gateway_transfer_reconnect_and_delete(tmp_path: Path):
    schema = "gw_machine_" + uuid4().hex
    namespace = "gw_machine:" + uuid4().hex
    settings = Settings(
        database_schema=schema,
        valkey_namespace=namespace,
        control_token="integration-admin",
        session_signing_key="integration-signing",
        machine_tokens={"one": "integration-machine"},
        telegram_bot_token=None,
    )
    app = create_app(settings.model_copy(update={"frontends": []}))
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app, log_level="warning", ws_max_size=1_048_576, ws_per_message_deflate=False
        )
    )
    serving = asyncio.create_task(server.serve(sockets=[listener]))
    paths = resolve_paths(
        state_dir=tmp_path / "state",
        data_dir=tmp_path / "data",
        runtime_dir=tmp_path / "run",
    )
    cli = [sys.executable, "-c", "from kapy.cli import main; main()"]
    daemon = None
    try:
        async with asyncio.timeout(60):
            while not server.started:
                if serving.done():
                    await serving
                    pytest.fail("Control server exited before startup")
                await asyncio.sleep(0.02)
            model_config = await register_model(app.state.control, base_url="http://127.0.0.1:9/v1")
            daemon = await asyncio.create_subprocess_exec(
                *cli,
                "server",
                env={
                    "PATH": os.defpath,
                    "PYTHONPATH": "/workspace/src",
                    "KAPY_CONTROL_URL": f"http://127.0.0.1:{port}",
                    "KAPY_MACHINE_ID": "one",
                    "KAPY_MACHINE_TOKEN": "integration-machine",
                    "KAPY_EXECUTION_STATE_DIR": str(paths.state_dir),
                    "KAPY_EXECUTION_DATA_DIR": str(paths.data_dir),
                    "KAPY_EXECUTION_RUNTIME_DIR": str(paths.runtime_dir),
                    "KAPY_CHILD_ENV": json.dumps({"PYTHONPATH": "/workspace/src"}),
                },
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            while "one" not in app.state.machines.connections:
                if daemon.returncode is not None:
                    _, error = await daemon.communicate()
                    pytest.fail(f"CLI daemon exited: {error.decode()}")
                await asyncio.sleep(0.02)

            async with httpx2.AsyncClient(trust_env=False) as http:

                async def rpc(method: str, params: dict[str, Any]) -> Any:
                    response = await http.post(
                        f"http://127.0.0.1:{port}/rpc",
                        headers={"Authorization": "Bearer integration-admin"},
                        json={"jsonrpc": "2.0", "id": "test", "method": method, "params": params},
                    )
                    response.raise_for_status()
                    body = response.json()
                    assert "error" not in body, body
                    return body["result"]

                created = await rpc(
                    "session.create",
                    {
                        "request_id": str(uuid4()),
                        "machine_ids": ["one"],
                        "default_machine_id": "one",
                        "config": model_config,
                    },
                )
                sid = created["session"]["id"]
                machines = app.state.machines

                async def run_cli(*arguments: str, success: bool = True) -> tuple[str, str]:
                    params = {
                        "session_id": sid,
                        "process_id": str(uuid4()),
                        "mode": "stdio",
                        "argv": [*cli, "control", *arguments],
                        "wait_ms": 1000,
                    }
                    result = await machines.call("one", "process.start", params)
                    while not result["process"]["output_complete"]:
                        result = await machines.call(
                            "one",
                            "process.wait",
                            {
                                "session_id": sid,
                                "process_id": params["process_id"],
                                "wait_ms": 1000,
                            },
                        )
                    stdout = base64.b64decode(result["output"]["stdout"]["data_base64"]).decode()
                    stderr = base64.b64decode(result["output"]["stderr"]["data_base64"]).decode()
                    assert (result["process"]["exit_code"] == 0) is success, stderr
                    return stdout, stderr

                # The daemon injects the session capability and socket into this real child.
                stdout, _ = await run_cli("session", "get")
                assert json.loads(stdout)["id"] == sid
                selection = model_config["model"]
                assert isinstance(selection, dict)
                selected_id = selection["model_id"]
                assert isinstance(selected_id, str)
                stdout, _ = await run_cli("provider", "model", "get", selected_id)
                selected = json.loads(stdout)
                assert selected["id"] == selected_id
                stdout, _ = await run_cli("provider", "models", selected["provider_id"])
                assert selected_id in {item["id"] for item in json.loads(stdout)["items"]}
                operator_catalog = await call_local_proxy(
                    paths.socket_path,
                    "provider.models",
                    {"provider_id": selected["provider_id"]},
                    auth={"kind": "user", "token": "integration-admin"},
                )
                assert isinstance(operator_catalog, dict)
                assert operator_catalog["items"] == json.loads(stdout)["items"]
                await run_cli(
                    "provider",
                    "model",
                    "update",
                    selected_id,
                    "--expected-revision",
                    str(selected["revision"]),
                    "--defaults",
                    '{"max_output_tokens":1024}',
                    success=False,
                )
                unrelated = await rpc(
                    "session.create", {"request_id": str(uuid4()), "config": model_config}
                )
                await run_cli(
                    "--session", unrelated["session"]["id"], "session", "get", success=False
                )

                cwd = paths.session_cwd(sid)
                source = cwd / "example-skill"
                source.mkdir()
                (source / "SKILL.md").write_text(
                    "---\nname: example-skill\ndescription: Test bounded transfers.\n"
                    "---\nDo a test.\n"
                )
                asset = os.urandom(150_000)  # Incompressible: exercise multiple 64 KiB chunks.
                (source / "asset.bin").write_bytes(asset)
                stdout, stderr = await run_cli("skill", "upload", str(source))
                info = json.loads(stdout)
                assert info["archive_bytes"] > 2 * 65_536
                assert "Request ID:" in stderr and "Archive:" in stderr
                stdout, _ = await run_cli(
                    "skill",
                    "upload",
                    str(source),
                    "--skill-id",
                    info["id"],
                    "--expected-revision",
                    "1",
                )
                assert json.loads(stdout)["revision"] == 2
                await run_cli(
                    "skill", "delete", info["id"], "--expected-revision", "1", success=False
                )
                stdout, _ = await run_cli(
                    "skill",
                    "download",
                    info["id"],
                    str(cwd / "download"),
                    "--expected-revision",
                    "2",
                )
                downloaded = json.loads(stdout)
                assert (Path(downloaded["destination"]) / "asset.bin").read_bytes() == asset
                assert downloaded["revision"] == 2

                previous = machines.connections["one"]
                await previous.peer.aclose()
                async with machines._changed:
                    await machines._changed.wait_for(
                        lambda: machines.connections.get("one") not in (None, previous)
                    )
                # Proactive ensure must finish on the new connection before the proxy serves.
                stdout, _ = await run_cli("session", "get")
                assert json.loads(stdout)["id"] == sid

                await rpc("session.delete", {"session_id": sid, "request_id": str(uuid4())})
                while True:
                    rows = await app.state.metadata.rows(
                        "SELECT state FROM gateway_session_cleanup WHERE session_id=%s",
                        (UUID(sid),),
                    )
                    if rows and rows[0]["state"] == "complete":
                        break
                    await asyncio.sleep(0.02)
                assert not cwd.exists()
    finally:
        if daemon is not None and daemon.returncode is None:
            daemon.terminate()
            await asyncio.wait_for(daemon.communicate(), 10)
        server.should_exit = True
        await asyncio.wait_for(serving, 10)
        listener.close()
        async with await psycopg.AsyncConnection.connect(DATABASE) as conn:
            await conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        async with Valkey.from_url(VALKEY) as client:
            keys = [key async for key in client.scan_iter(match=namespace + "*")]
            if keys:
                await client.delete(*keys)

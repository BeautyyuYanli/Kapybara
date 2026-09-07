"""Real execution tests: run exclusively in the dedicated machine container."""

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx2
import pytest
import pytest_asyncio

from kapy.execution.daemon import MachineService
from kapy.execution.paths import resolve_paths
from kapy.execution.store import ExecutionStore
from kapy.rpc import RpcError


@pytest_asyncio.fixture
async def service(tmp_path: Path):
    paths = resolve_paths(
        state_dir=tmp_path / "state", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run"
    )
    async with (
        ExecutionStore(paths, "machine") as store,
        httpx2.AsyncClient(trust_env=False) as http,
    ):
        service = MachineService(store, http_client=http, child_env={"PATH": "/usr/bin:/bin"})
        await service.initialize()
        await service.handle(
            "session.ensure", {"session_id": "s", "session_token": "secret-session"}
        )
        try:
            yield service
        finally:
            await service.aclose()


def start(code: str, *, mode: str = "stdio", wait_ms: int = 1000) -> dict[str, Any]:
    return {
        "session_id": "s",
        "process_id": str(uuid4()),
        "mode": mode,
        "argv": [sys.executable, "-c", code],
        "wait_ms": wait_ms,
    }


def payload(update: Any, name: str = "stdout") -> bytes:
    return base64.b64decode(update["output"][name]["data_base64"])


@pytest.mark.asyncio
async def test_stdio_descendant_continues_after_leader_and_retry(service: MachineService):
    params = start(
        "import os,time; p=os.fork(); "
        "os._exit(0) if p else None; print('first',flush=True); "
        "time.sleep(2.3); print('last',flush=True)",
        wait_ms=50,
    )
    first: Any = await service.handle("process.start", params)
    assert first["process"]["state"] == "running"
    result: Any = await service.handle(
        "process.wait", {"session_id": "s", "process_id": params["process_id"], "wait_ms": 5000}
    )
    assert result["process"]["output_complete"] is True
    assert payload(result) == b"first\nlast\n"
    assert (await service.handle("process.start", params)) == {**result, "reason": "exited"}
    with pytest.raises(RpcError, match="different startup"):
        await service.handle("process.start", {**params, "argv": ["/bin/true"]})
    await service.handle("process.release", {"session_id": "s", "process_id": params["process_id"]})
    with pytest.raises(RpcError, match="released"):
        await service.handle("process.start", params)


@pytest.mark.asyncio
async def test_complete_64mib_spool_paged_hash(service: MachineService):
    params = start(
        "import os; b=b'x'*65536; [os.write(1,b) for _ in range(1024)]; os.write(2,b'error')",
        wait_ms=30000,
    )
    result: Any = await service.handle("process.start", params)
    assert result["process"]["output_complete"]
    cursor: dict[str, Any] = {"stdout": 0, "stderr": 0}
    digest = hashlib.sha256()
    while cursor["stdout"] < 64 * 1024 * 1024:
        result = cast(
            Any,
            await service.handle(
                "process.wait",
                {
                    "session_id": "s",
                    "process_id": params["process_id"],
                    "cursor": cursor,
                    "wait_ms": 0,
                },
            ),
        )
        chunk = payload(result)
        assert len(chunk) <= 65536
        digest.update(chunk)
        cursor = {name: result["output"][name]["next"] for name in cursor}
    expected = hashlib.sha256()
    for _ in range(1024):
        expected.update(b"x" * 65536)
    assert digest.digest() == expected.digest()
    assert result["output"]["stdout"]["eof"]


@pytest.mark.asyncio
async def test_pty_tail_resize_input_and_ctrlc(service: MachineService):
    params = start(
        "import os,time; os.write(1,b'x'*20000); print('READY',flush=True); "
        "s=input(); print('GOT:'+s,flush=True); time.sleep(30)",
        mode="pty",
    )
    first: Any = await service.handle("process.start", params)
    assert first["reason"] == "quiet"
    assert len(payload(first, "pty")) == 8192
    assert first["output"]["pty"]["truncated"]
    identity = {"session_id": "s", "process_id": params["process_id"]}
    await service.handle("process.resize", {**identity, "rows": 45, "cols": 100})
    written: Any = await service.handle(
        "process.write", {**identity, "data_base64": base64.b64encode(b"hello\n").decode()}
    )
    assert written["accepted_bytes"] == 6
    result: Any = await service.handle(
        "process.wait",
        {**identity, "wait_ms": 1000, "cursor": {"pty": first["output"]["pty"]["next"]}},
    )
    assert b"GOT:hello" in payload(result, "pty")
    await service.handle("process.write", {**identity, "data_base64": "Aw=="})
    result = cast(Any, await service.handle("process.wait", {**identity, "wait_ms": 3000}))
    assert result["process"]["state"] == "exited"
    assert result["process"]["exit_code"] != 0


@pytest.mark.asyncio
async def test_concurrency_kill_session_and_environment(service: MachineService):
    params = [start("import time; time.sleep(60)", wait_ms=0) for _ in range(16)]
    for item in params:
        await service.handle("process.start", item)
    with pytest.raises(RpcError, match="16"):
        await service.handle("process.start", start("pass", wait_ms=0))
    assert service.processes.active_count == 16
    result: Any = await service.handle("session.release", {"session_id": "s"})
    assert result["released"]
    assert service.processes.active_count == 0
    assert not service.store.paths.session_cwd("s").exists()
    with pytest.raises(RpcError):
        await service.handle("process.start", params[0])


@pytest.mark.asyncio
async def test_environment_is_explicit_and_token_refresh(service: MachineService, monkeypatch):
    monkeypatch.setenv("PARENT_SECRET", "must-not-leak")
    await service.handle("session.ensure", {"session_id": "s", "session_token": "new-token"})
    result: Any = await service.handle(
        "process.start", start("import os,json; print(json.dumps(dict(os.environ)))")
    )
    env = json.loads(payload(result))
    assert "PARENT_SECRET" not in env
    assert env["KAPY_SESSION_TOKEN"] == "new-token"
    with pytest.raises(RpcError, match="reserved"):
        await service.handle(
            "process.start", {**start("pass"), "env": {"KAPY_MACHINE_TOKEN": "bad"}}
        )


@pytest.mark.asyncio
async def test_cancelled_observer_does_not_cancel_process(service: MachineService):
    params = start("import time; time.sleep(.3); print('finished')", wait_ms=30000)
    observer = asyncio.create_task(service.handle("process.start", params))
    await asyncio.sleep(0.1)
    observer.cancel()
    await asyncio.gather(observer, return_exceptions=True)
    result: Any = await service.handle(
        "process.wait", {"session_id": "s", "process_id": params["process_id"], "wait_ms": 2000}
    )
    assert payload(result) == b"finished\n"
    assert result["process"]["output_complete"]


@pytest.mark.asyncio
async def test_kill_after_leader_exit_and_closed_outputs(service: MachineService):
    params = start(
        "import os,time; p=os.fork(); os._exit(0) if p else None; "
        "os.close(1); os.close(2); time.sleep(60)",
        wait_ms=100,
    )
    first = cast(Any, await service.handle("process.start", params))
    assert first["process"]["state"] == "running"
    killed = cast(
        Any,
        await service.handle(
            "process.kill", {"session_id": "s", "process_id": params["process_id"]}
        ),
    )
    assert killed["state"] == "killed"
    assert service.processes.active_count == 0


@pytest.mark.asyncio
async def test_start_failure_and_invalid_cursor_leave_service_usable(service: MachineService):
    params: dict[str, Any] = {**start("pass"), "argv": ["/definitely/not/a/program"]}
    result = cast(Any, await service.handle("process.start", params))
    assert result["process"]["state"] == "failed"
    assert not result["process"]["output_complete"]
    with pytest.raises(RpcError):
        await service.handle(
            "process.wait",
            {
                "session_id": "s",
                "process_id": params["process_id"],
                "cursor": {"stdout": 1, "stderr": 0},
            },
        )
    assert service.processes.active_count == 0
    await service.handle("process.release", {"session_id": "s", "process_id": params["process_id"]})

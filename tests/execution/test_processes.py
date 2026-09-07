"""Real execution tests: run exclusively in the dedicated machine container."""

import asyncio
import base64
import hashlib
import json
import os
import signal
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
    stderr = bytearray()
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
        stderr.extend(payload(result, "stderr"))
        cursor = {name: result["output"][name]["next"] for name in cursor}
    expected = hashlib.sha256()
    for _ in range(1024):
        expected.update(b"x" * 65536)
    assert digest.digest() == expected.digest()
    assert result["output"]["stdout"]["eof"]
    assert bytes(stderr) == b"error"
    assert result["output"]["stderr"]["eof"]


@pytest.mark.asyncio
async def test_pty_tail_resize_input_and_ctrlc(service: MachineService):
    params = start(
        "import os,time; os.write(1,b'x'*20000); print('READY',flush=True); "
        "s=input(); print('GOT:'+s,flush=True); "
        "print('SIZE:'+str(os.get_terminal_size()),flush=True); time.sleep(30)",
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
    assert b"SIZE:os.terminal_size(columns=100, lines=45)" in payload(result, "pty")
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
        "print(os.getpid(),flush=True); os.close(1); os.close(2); time.sleep(60)",
        wait_ms=100,
    )
    first = cast(Any, await service.handle("process.start", params))
    descendant_pid = int(payload(first).strip())

    def alive() -> bool:
        try:
            state = Path(f"/proc/{descendant_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
            return state != "Z"
        except FileNotFoundError:
            return False

    try:
        assert first["process"]["state"] == "running"
        assert alive()
        killed = cast(
            Any,
            await service.handle(
                "process.kill", {"session_id": "s", "process_id": params["process_id"]}
            ),
        )
        assert killed["state"] == "killed"
        assert service.processes.active_count == 0
        async with asyncio.timeout(2):
            while alive():  # noqa: ASYNC110 - observing an OS descendant, not an asyncio task
                await asyncio.sleep(0.01)
    finally:
        if alive():
            try:
                os.kill(descendant_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


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


@pytest.mark.asyncio
async def test_startup_database_failure_still_reaps_and_unblocks_cleanup(
    service: MachineService, monkeypatch
):
    original_save = service.store.save_process
    saves = 0

    async def fail_after_intent(*args, **kwargs):
        nonlocal saves
        saves += 1
        if saves >= 2:
            raise OSError("database write failed")
        return await original_save(*args, **kwargs)

    monkeypatch.setattr(service.store, "save_process", fail_after_intent)
    params = start("import time; time.sleep(60)", wait_ms=0)
    async with asyncio.timeout(3):
        with pytest.raises(OSError, match="database write failed"):
            await service.handle("process.start", params)
        assert service.processes.active_count == 0
        entry = service.processes._entries["s", params["process_id"]]
        assert entry.child is not None and entry.child.returncode is not None
        assert entry.info["state"] == "failed"
        assert entry.info["error"] is not None
        assert not entry.fds and entry.master == -1
        await service.processes.aclose()
        monkeypatch.setattr(service.store, "save_process", original_save)
        released = await service.handle("session.release", {"session_id": "s"})
        assert released == {"session_id": "s", "released": True}


@pytest.mark.asyncio
async def test_spool_fsync_failure_reports_incomplete(service: MachineService, monkeypatch):
    import kapy.execution.processes as processes

    def fail_fsync(fd):
        raise OSError("simulated fsync failure")

    monkeypatch.setattr(processes.os, "fsync", fail_fsync)
    result = cast(Any, await service.handle("process.start", start("print('collected')")))
    assert result["process"]["state"] == "failed"
    assert result["process"]["output_complete"] is False
    assert result["process"]["error"]["kind"] == "io_error"
    assert payload(result) == b"collected\n"
    assert service.processes.active_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "shortened"])
async def test_recovery_detects_damaged_terminal_spool(service: MachineService, damage: str):
    from kapy.execution.processes import ProcessManager

    params = start("print('durable output')")
    result = cast(Any, await service.handle("process.start", params))
    assert result["process"]["output_complete"] is True
    entry = service.processes._entries["s", params["process_id"]]
    spool = entry.root / "stdout"
    if damage == "missing":
        spool.unlink()
    else:
        spool.write_bytes(b"dur")
    recovered = ProcessManager(service.store)
    await recovered.initialize()
    try:
        result = cast(
            Any,
            await recovered.handle(
                "process.wait",
                {"session_id": "s", "process_id": params["process_id"], "wait_ms": 0},
            ),
        )
        assert result["process"]["output_complete"] is False
        assert result["process"]["error"]["kind"] == "io_error"
        assert payload(result) == (b"" if damage == "missing" else b"dur")
        records = await service.store.process_records()
        assert cast(Any, records[0]["info"])["output_complete"] is False
    finally:
        await recovered.aclose()


@pytest.mark.asyncio
async def test_recovery_retries_interrupted_process_release(service: MachineService, monkeypatch):
    import kapy.execution.processes as processes

    params = start("print('will be released')")
    await service.handle("process.start", params)
    entry = service.processes._entries["s", params["process_id"]]
    remove = processes._remove_root

    def fail_remove(root):
        raise OSError("simulated directory cleanup failure")

    monkeypatch.setattr(processes, "_remove_root", fail_remove)
    with pytest.raises(OSError, match="cleanup failure"):
        await service.handle(
            "process.release", {"session_id": "s", "process_id": params["process_id"]}
        )
    assert entry.root.exists()
    monkeypatch.setattr(processes, "_remove_root", remove)
    recovered = processes.ProcessManager(service.store)
    await recovered.initialize()
    await recovered.initialize()  # Cleanup remains harmless when already removed.
    assert not entry.root.exists()
    with pytest.raises(RpcError, match="released"):
        await recovered.handle("process.start", params)
    await recovered.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("boot", ["matching", "different", "missing"])
async def test_recovery_kills_only_same_boot_and_start_identity(service: MachineService, boot: str):
    import kapy.execution.processes as processes

    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)", start_new_session=True
    )
    process_id = str(uuid4())
    meta: dict[str, Any] = {
        "pid": child.pid,
        "identity": processes._identity(child.pid),
        "sizes": {"pty": 0},
        "tail": "",
    }
    if boot != "missing":
        meta["boot_id"] = processes._boot_id() if boot == "matching" else "another-boot"
    info: dict[str, Any] = {
        "session_id": "s",
        "process_id": process_id,
        "mode": "pty",
        "cwd": str(service.store.paths.session_cwd("s")),
        "state": "running",
        "exit_code": None,
        "output_complete": False,
        "error": None,
    }
    await service.store.save_process("interrupted", info, meta)
    recovered = processes.ProcessManager(service.store)
    try:
        await recovered.initialize()
        result = cast(
            Any,
            await recovered.handle(
                "process.wait", {"session_id": "s", "process_id": process_id, "wait_ms": 0}
            ),
        )
        assert result["process"]["state"] == "lost"
        if boot == "matching":
            assert await asyncio.wait_for(child.wait(), 2) == -9
        else:
            assert child.returncode is None
    finally:
        if child.returncode is None:
            child.kill()
        await child.wait()
        await recovered.aclose()

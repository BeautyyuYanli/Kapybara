"""Measure complete stdio output and 16 independent interactive PTYs in Docker."""

import asyncio
import base64
import hashlib
import json
import resource
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx2

from kapy.execution.daemon import MachineService
from kapy.execution.paths import resolve_paths
from kapy.execution.store import ExecutionStore
from kapy.rpc import JsonParams


async def benchmark() -> None:
    with tempfile.TemporaryDirectory(prefix="kapy-machine-bench-") as directory:
        root = Path(directory)
        paths = resolve_paths(
            state_dir=root / "state", data_dir=root / "data", runtime_dir=root / "run"
        )
        async with (
            ExecutionStore(paths, "benchmark") as store,
            httpx2.AsyncClient(trust_env=False) as http,
        ):
            machine = MachineService(store, http_client=http, child_env={"PATH": "/usr/bin:/bin"})
            await machine.initialize()
            session_id = str(uuid4())

            async def call(method: str, **params: Any) -> Any:
                return await machine.handle(
                    method, cast(JsonParams, {"session_id": session_id, **params})
                )

            try:
                await call("session.ensure", session_token="benchmark-only")
                baseline_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                started = time.perf_counter()
                process_id = str(uuid4())
                status = await call(
                    "process.start",
                    process_id=process_id,
                    mode="stdio",
                    argv=[
                        sys.executable,
                        "-c",
                        "import os; block=b'x'*65536; "
                        "[os.write(1,block) for _ in range(1024)]; os.write(2,b'STDERR_OK')",
                    ],
                    wait_ms=30000,
                )
                async with asyncio.timeout(60):
                    while status["process"]["state"] == "running":
                        status = await call("process.wait", process_id=process_id, wait_ms=1000)
                    if status["process"]["exit_code"] != 0:
                        raise AssertionError("Output producer did not exit successfully")
                    if not status["process"]["output_complete"]:
                        raise AssertionError("Output was reported incomplete")
                    cursors = {"stdout": 0, "stderr": 0}
                    digest = hashlib.sha256()
                    stderr = bytearray()
                    while True:
                        status = await call(
                            "process.wait", process_id=process_id, cursor=cursors, wait_ms=0
                        )
                        for stream in cursors:
                            chunk = status["output"][stream]
                            data = base64.b64decode(chunk["data_base64"], validate=True)
                            if chunk["truncated"] or chunk["start"] != cursors[stream]:
                                raise AssertionError("Spool pagination omitted bytes")
                            if stream == "stdout":
                                digest.update(data)
                            else:
                                stderr.extend(data)
                            cursors[stream] = chunk["next"]
                        if all(status["output"][stream]["eof"] for stream in cursors):
                            break
                expected = hashlib.sha256()
                for _ in range(1024):
                    expected.update(b"x" * 65536)
                if cursors["stdout"] != 64 * 1024 * 1024 or digest.digest() != expected.digest():
                    raise AssertionError("64 MiB output hash or length mismatch")
                if stderr != b"STDERR_OK":
                    raise AssertionError("stderr did not match")
                stdio_seconds = time.perf_counter() - started
                stdio_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                await call("process.release", process_id=process_id)

                started = time.perf_counter()
                jobs = [str(uuid4()) for _ in range(16)]
                code = (
                    "import sys; sys.stdout.write('x'*20000+'\\nREADY\\n'); "
                    "sys.stdout.flush(); line=input(); print('GOT:'+line,flush=True)"
                )
                async with asyncio.timeout(30):
                    opened = await asyncio.gather(
                        *(
                            call(
                                "process.start",
                                process_id=job,
                                mode="pty",
                                argv=[sys.executable, "-c", code],
                                wait_ms=1000,
                            )
                            for job in jobs
                        )
                    )
                    max_tail = 0
                    for status in opened:
                        chunk = status["output"]["pty"]
                        retained = base64.b64decode(chunk["data_base64"], validate=True)
                        max_tail = max(max_tail, len(retained))
                        if (
                            status["process"]["state"] != "running"
                            or len(retained) != 8192
                            or not chunk["truncated"]
                            or b"READY" not in retained
                        ):
                            raise AssertionError("Interactive job or bounded tail was not ready")

                    async def finish(index: int, job: str) -> None:
                        marker = f"TASK_{index:02d}"
                        await call(
                            "process.write",
                            process_id=job,
                            data_base64=base64.b64encode((marker + "\n").encode()).decode(),
                        )
                        cursor = opened[index]["output"]["pty"]["next"]
                        output = bytearray()
                        while True:
                            status = await call(
                                "process.wait",
                                process_id=job,
                                cursor={"pty": cursor},
                                wait_ms=1000,
                            )
                            chunk = status["output"]["pty"]
                            if chunk["truncated"]:
                                raise AssertionError("Interactive reply unexpectedly truncated")
                            output.extend(base64.b64decode(chunk["data_base64"], validate=True))
                            cursor = chunk["next"]
                            if status["process"]["state"] != "running" and chunk["eof"]:
                                if status["process"]["exit_code"] != 0:
                                    raise AssertionError("Interactive job failed")
                                break
                        if ("GOT:" + marker).encode() not in output:
                            raise AssertionError("Interactive reply was missing or crossed jobs")
                        if any(
                            f"GOT:TASK_{other:02d}".encode() in output
                            for other in range(16)
                            if other != index
                        ):
                            raise AssertionError("Interactive outputs crossed jobs")
                        await call("process.release", process_id=job)

                    await asyncio.gather(*(finish(index, job) for index, job in enumerate(jobs)))
                pty_seconds = time.perf_counter() - started
                peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                await call("session.release")
                if paths.session_cwd(session_id).exists():
                    raise AssertionError("Released session working directory remains")
                print(
                    json.dumps(
                        {
                            "stdio_bytes": cursors["stdout"],
                            "stdio_hash_matches": True,
                            "stdio_seconds": round(stdio_seconds, 3),
                            "stdio_peak_rss_growth_kib": max(0, stdio_rss - baseline_rss),
                            "interactive_ptys": len(jobs),
                            "pty_tail_bytes": max_tail,
                            "pty_seconds": round(pty_seconds, 3),
                            "pty_peak_rss_growth_kib": max(0, peak_rss - stdio_rss),
                            "process_peak_rss_kib": peak_rss,
                            "session_cleanup": True,
                        }
                    )
                )
            finally:
                await machine.aclose()


if __name__ == "__main__":
    if not Path("/.dockerenv").is_file():
        raise RuntimeError("Run this benchmark inside the dedicated Docker machine")
    asyncio.run(benchmark())

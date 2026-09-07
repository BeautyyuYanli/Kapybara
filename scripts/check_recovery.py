"""Crash an isolated Docker control plane during a real machine task and verify recovery."""

import argparse
import asyncio
import json
import os
import re
import shlex
from typing import Any
from uuid import uuid4

import httpx2


async def check(machine: str, control: str, daemon: str) -> None:
    base = os.environ.get("KAPY_CONTROL_URL", "http://127.0.0.1:8000").rstrip("/")

    async def docker(*arguments: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "docker", *arguments, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        output, _ = await process.communicate()
        if process.returncode:
            raise RuntimeError(f"Docker {arguments[0]} failed")
        return output.decode().strip()

    async with httpx2.AsyncClient(
        headers={"Authorization": "Bearer " + os.environ["KAPY_CONTROL_TOKEN"]}, timeout=40
    ) as client:

        async def rpc(method: str, **params: Any) -> Any:
            identity = uuid4().hex
            response = await client.post(
                base + "/rpc",
                json={"jsonrpc": "2.0", "id": identity, "method": method, "params": params},
            )
            response.raise_for_status()
            body = response.json()
            if body.get("id") != identity or "error" in body:
                raise AssertionError(f"{method} failed or returned a mismatched response ID")
            return body["result"]

        async def ready() -> None:
            async with asyncio.timeout(60):
                while True:
                    try:
                        response = await client.get(base + "/docs", timeout=2)
                        if response.status_code == 200:
                            return
                    except httpx2.HTTPError:
                        pass
                    await asyncio.sleep(0.2)

        session = await rpc(
            "session.create",
            request_id=str(uuid4()),
            title="Kapy crash recovery acceptance",
            machine_ids=[machine],
            default_machine_id=machine,
            config={},
        )
        sid = session["session"]["id"]
        counter = "/tmp/kapy-recovery-" + uuid4().hex
        stopped = False
        try:
            request_id = str(uuid4())
            code = (
                "import os,secrets,time; "
                f"fd=os.open({counter!r},os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600); "
                "os.write(fd,b'x'); os.close(fd); time.sleep(20); "
                "print('KAPY_RECOVERY_'+secrets.token_hex(16),flush=True)"
            )
            await rpc(
                "session.input",
                session_id=sid,
                request_id=request_id,
                mode="queue",
                payload=(
                    "Use process_start in stdio mode on the default machine to execute this "
                    "exact command once: "
                    + shlex.join(["python", "-c", code])
                    + ". If it is still running, wait for that same process to finish. "
                    "Do not start it again. Reply with only the random KAPY_RECOVERY_ marker "
                    "that the process printed."
                ),
            )
            read_count = (
                "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
                "print(p.stat().st_size if p.exists() else 0)"
            )
            async with asyncio.timeout(120):
                while True:
                    count = await docker("exec", daemon, "python", "-c", read_count, counter)
                    if count != "0":
                        break
                    await asyncio.sleep(0.2)
            started = await docker("inspect", "-f", "{{.State.StartedAt}}", daemon)
            await docker("kill", "--signal", "KILL", control)
            stopped = True
            await docker("start", control)
            stopped = False
            await ready()
            if await docker("inspect", "-f", "{{.State.StartedAt}}", daemon) != started:
                raise AssertionError("The execution daemon restarted with the control plane")
            async with asyncio.timeout(180):
                while True:
                    status = await rpc(
                        "session.wait", session_id=sid, request_id=request_id, wait_seconds=30
                    )
                    if status["completion"] is not None:
                        break
            completion = status["completion"]
            marker = completion["output"].strip()
            if completion["outcome"] != "completed" or not re.fullmatch(
                r"KAPY_RECOVERY_[0-9a-f]{32}", marker
            ):
                raise AssertionError(
                    "The interrupted task did not complete with its machine marker"
                )
            count = int(await docker("exec", daemon, "python", "-c", read_count, counter))
            if count != 1:
                raise AssertionError(f"The machine command executed {count} times")
            records: list[dict[str, Any]] = []
            cursor = None
            while True:
                page = await rpc("history.read", session_id=sid, after=cursor, limit=200)
                records.extend(page["items"])
                cursor = page["next_cursor"]
                if not page["has_more"]:
                    break
            attempts = {r["attempt"] for r in records if r["attempt"] is not None}
            if max(attempts, default=0) < 2:
                raise AssertionError("The history does not show a resumed run attempt")
            if not any(
                part.get("part_kind") == "tool-return" and marker in json.dumps(part)
                for record in records
                if record["kind"] == "model_request" and isinstance(record["data"], dict)
                for part in record["data"].get("parts", [])
            ):
                raise AssertionError("Recovered marker is absent from durable tool returns")
            print(
                json.dumps(
                    {
                        "session_id": sid,
                        "control_crashed": True,
                        "daemon_restarted": False,
                        "command_executions": count,
                        "attempts": sorted(attempts),
                        "durable_tool_result_matches_reply": True,
                    }
                )
            )
        finally:
            if stopped:
                await docker("start", control)
                await ready()
            await rpc("session.delete", session_id=sid, request_id=str(uuid4()))
            await docker(
                "exec",
                daemon,
                "python",
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).unlink(missing_ok=True)",
                counter,
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--machine", required=True)
    parser.add_argument("--control-container", required=True)
    parser.add_argument("--daemon-container", required=True)
    args = parser.parse_args()
    asyncio.run(check(args.machine, args.control_container, args.daemon_container))

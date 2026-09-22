"""Exercise the vendored Python SDK against its matching Go server and tmux.

Build via the outer Makefile first. Every test owns a private tmux socket and
SQLite directory; only its HTTP process is killed during restart verification.
"""

import os
import socket
import subprocess
from pathlib import Path

import anyio
import httpx2
import pytest
from shellctl import JobMode, JobStatusName, ShellctlClient, ShellctlClientError

BINARIES = Path(__file__).resolve().parents[1] / "server" / "bin"


@pytest.mark.anyio
@pytest.mark.integration
async def test_sdk_recovers_jobs_after_server_restart(tmp_path: Path) -> None:
    binaries = BINARIES
    assert (binaries / "shellctl").is_file(), "Run make -C packages/shellctl build-server"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    # Share only ordinary environment settings; test jobs need no user secrets.
    environment = {key: os.environ[key] for key in ("HOME", "USER", "LANG") if key in os.environ}
    environment.update(PATH=f"{binaries}:/usr/bin:/bin", XDG_DATA_HOME=str(tmp_path))
    tmux_socket = tmp_path / "shellctl" / "runtime" / "tmux.sock"
    process: subprocess.Popen[bytes] | None = None

    with (tmp_path / "server.log").open("wb") as log:

        def launch() -> subprocess.Popen[bytes]:
            return subprocess.Popen(
                [str(binaries / "shellctl"), "serve", "--listen", f"127.0.0.1:{port}"],
                env=environment,
                stdout=log,
                stderr=log,
            )

        async def ready(client: ShellctlClient) -> None:
            with anyio.fail_after(10):
                while True:
                    assert process is not None and process.poll() is None
                    try:
                        assert (await client.health()).status == "ok"
                        return
                    except httpx2.TransportError:
                        await anyio.sleep(0.05)

        try:
            process = launch()
            async with ShellctlClient(
                f"http://127.0.0.1:{port}",
                token="",
                idle_flush_seconds=0.05,
            ) as client:
                await ready(client)
                stdio = await client.run(
                    "printf 'before\\n'; sleep 1; printf 'after\\n'; "
                    "printf 'private-stderr\\n' >&2; exit 7",
                    cwd=str(tmp_path),
                    mode=JobMode.STDIO,
                    timeout=0.2,
                )
                assert not stdio.done
                process.kill()
                process.wait(timeout=5)
                await anyio.sleep(2)
                process = launch()
                await ready(client)
                recovered = await client.wait(stdio.job_id, offset=0, timeout=1)
                assert recovered.done and recovered.exit_code == 7
                assert recovered.output == "before\nafter\n"

                pty = await client.run(
                    "printf 'ready\\n'; read -r answer; printf 'echo:%s\\n' \"$answer\"; exit 9",
                    cwd=str(tmp_path),
                    timeout=0.2,
                )
                assert not pty.done
                process.kill()
                process.wait(timeout=5)
                process = launch()
                await ready(client)
                assert (await client.status(pty.job_id)).status == JobStatusName.RUNNING
                await client.input(pty.job_id, "resume-ok\n", offset=pty.offset, timeout=2)
                with anyio.fail_after(5):
                    while True:
                        if (await client.wait(pty.job_id, offset=0, timeout=0.2)).done:
                            break
                resumed = await client.tail(pty.job_id)
                assert resumed.exit_code == 9 and "echo:resume-ok\n" in resumed.output
                assert {job.job_id for job in await client.list_jobs()} == {
                    stdio.job_id,
                    pty.job_id,
                }
                assert (await client.delete(stdio.job_id)).deleted
                with pytest.raises(ShellctlClientError) as missing:
                    await client.status(stdio.job_id)
                assert missing.value.status_code == 404
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            await anyio.run_process(
                ["tmux", "-S", str(tmux_socket), "kill-server"],
                env=environment,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )

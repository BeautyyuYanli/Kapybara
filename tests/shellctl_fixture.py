"""Private shellctl service fixture for durable shell layer tests."""

import asyncio
import os
import socket
import subprocess
from pathlib import Path

import anyio
import httpx2
import pytest_asyncio
from shellctl import ShellctlClient


@pytest_asyncio.fixture
async def shell_server(tmp_path):
    """A private server, SQLite directory and tmux socket; never the deployment."""
    binaries = Path(os.environ["KAPY_SHELLCTL_TEST_BIN_DIR"])
    assert (binaries / "shellctl").is_file(), "Run make -C packages/shellctl build-server"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    environment = {key: os.environ[key] for key in ("HOME", "USER", "LANG") if key in os.environ}
    # The application image intentionally uses nologin; tmux needs a real shell.
    environment.update(
        PATH=f"{binaries}:/usr/bin:/bin", XDG_DATA_HOME=str(tmp_path), SHELL="/bin/bash"
    )
    tmux_socket = tmp_path / "shellctl/runtime/tmux.sock"
    with (tmp_path / "server.log").open("wb") as log:
        process = await asyncio.create_subprocess_exec(
            str(binaries / "shellctl"),
            "serve",
            "--listen",
            f"127.0.0.1:{port}",
            env=environment,
            stdout=log,
            stderr=log,
        )
        try:
            async with ShellctlClient(f"http://127.0.0.1:{port}", token="") as client:
                with anyio.fail_after(10):
                    while True:
                        assert process.returncode is None
                        try:
                            await client.health()
                            break
                        except httpx2.TransportError:
                            await anyio.sleep(0.05)
                yield client, tmp_path
        finally:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), 5)
            await anyio.run_process(
                ["tmux", "-S", str(tmux_socket), "kill-server"],
                env=environment,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

"""Unit tests for async `k-cron` bash helpers."""

from __future__ import annotations

from pathlib import Path
import shlex
import sys
from typing import Any

import anyio

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from k.agent import bash_helper


def test_base_session_next_and_interrupt(monkeypatch: Any) -> None:
    """BashSession should accept stdin, drain outputs, and expose return code."""

    del monkeypatch  # not used; kept for parity with other tests

    code = (
        "import sys\n"
        "sys.stdout.write('ready\\n'); sys.stdout.flush()\n"
        "line = sys.stdin.readline()\n"
        "sys.stdout.write('got:' + line); sys.stdout.flush()\n"
        "sys.stderr.write('err\\n'); sys.stderr.flush()\n"
    )
    command = shlex.join([sys.executable, "-u", "-c", code])

    async def _scenario() -> tuple[bytes, bytes, int | None]:
        async with bash_helper.BashSession(command) as session:
            stdout_parts: list[bytes] = []
            stderr_parts: list[bytes] = []

            # Send stdin so the subprocess can complete within the 10s timeout.
            out, err, rc = await session.next(b"hello\n")
            stdout_parts.append(out)
            stderr_parts.append(err)

            await session.interrupt()
        return (b"".join(stdout_parts), b"".join(stderr_parts), rc)

    stdout, stderr, rc = anyio.run(_scenario)

    assert b"ready\n" in stdout
    assert b"got:hello\n" in stdout
    assert b"err\n" in stderr
    assert rc == 0

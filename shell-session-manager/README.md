# shell-session-manager

`shell-session-manager` provides async subprocess sessions that can be resumed
across multiple calls. It drains stdout/stderr in background asyncio tasks,
supports incremental stdin writes, exposes short session ids, and manages
best-effort cleanup for long-lived command sessions.

Command construction is intentionally out of scope: callers should pass the
exact command string they want to execute, including any shell, SSH, PTY, or
environment bootstrap wrappers.

## Usage

```python
import anyio

from shell_session_manager import ShellSession, ShellSessionOptions


async def main() -> None:
    async with ShellSession(
        "python -u -c 'import sys; print(\"ready\"); line = sys.stdin.readline(); print(line.upper(), end=\"\")'",
        options=ShellSessionOptions(timeout_seconds=1),
    ) as session:
        stdout, stderr, code = await session.next()
        print(stdout.decode(), stderr.decode(), code)  # ready\n, "", None

        stdout, stderr, code = await session.next(b"hello\n")
        print(stdout.decode(), stderr.decode(), code)  # HELLO\n, "", 0


anyio.run(main)
```

For multiple long-lived sessions, use `ShellSessionManager`:

```python
import anyio

from shell_session_manager import ShellSessionManager


async def main() -> None:
    async with ShellSessionManager() as manager:
        session_id = await manager.new_shell("python -c 'print(42)'")
        stdout, stderr, returncode = await manager.next(session_id)
        print(stdout.decode(), stderr.decode(), returncode)


anyio.run(main)
```

## shellctl server

Run the HTTP API locally with:

```bash
pdm run shellctl serve --listen 127.0.0.1:8765
```

Pass `--auth-token your-token` when you want bearer auth enforced. `shellctl
serve` also reads `SHELLCTL_AUTH_TOKEN`, so you can export the token instead of
passing the flag. Leave the flag/env var unset or empty to start without
requiring an Authorization header.

## shellctl direct CLI

For one-shot local job control, use the same `shellctl` console script without
starting `shellctl serve`:

```bash
pdm run shellctl health
pdm run shellctl run 'echo Hello World'
pdm run shellctl wait <job-id> --offset <offset>
pdm run shellctl input <job-id> $'hello\n' --offset <offset>
pdm run shellctl tail <job-id>
pdm run shellctl status <job-id>
pdm run shellctl list --status running
pdm run shellctl terminate <job-id>
pdm run shellctl delete <job-id> --force
```

These commands call `ShellctlService` directly, use the local state/runtime
directories, and emit compact JSON on stdout.

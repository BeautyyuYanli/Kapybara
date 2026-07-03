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

## shellctl network CLI

Job-management commands now talk to a running `shellctl serve` instance. Start
the server first, then point the CLI at it with `--base-url` or
`SHELLCTL_BASE_URL` when you are not using the default `http://127.0.0.1:8765`.
Authenticated job commands also read `SHELLCTL_AUTH_TOKEN`, and `--auth-token`
overrides the environment when you need a different bearer token for one call.

```bash
export SHELLCTL_BASE_URL=http://127.0.0.1:8765
export SHELLCTL_AUTH_TOKEN=your-token
```

The public health endpoint ignores auth configuration, but the option is still
accepted for a consistent CLI surface.

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

These commands emit compact JSON on stdout. `shellctl list` prints a bare JSON
array matching `ShellctlClient.list_jobs()`, while the other commands print one
JSON object each. SDK failures, HTTP errors, and network errors emit compact
JSON error objects on stderr and exit non-zero.

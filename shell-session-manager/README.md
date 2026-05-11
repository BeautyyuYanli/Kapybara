# shell-session-manager

`shell-session-manager` provides async subprocess sessions that can be resumed
across multiple calls. It drains stdout/stderr in background asyncio tasks,
supports incremental stdin writes, exposes short session ids, and manages
best-effort cleanup for long-lived command sessions.

Command construction is intentionally out of scope: callers should pass the
exact command string they want to execute, including any shell, SSH, PTY, or
environment bootstrap wrappers.

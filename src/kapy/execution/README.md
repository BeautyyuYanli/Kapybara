# Execution daemon, processes, files and local proxy

`from kapy.execution import DaemonConfig, run_daemon` is the public launch API.
`await run_daemon(config, stop=event)` owns storage, managers, the private Unix
proxy and outbound WebSocket until the optional AnyIO event is set. Cancellation
also closes these resources. Config takes the full Gateway machine URL, its
independent `SecretStr` machine token and optional XDG roots, explicit child
environment and idle intervals. Loopback `ws` is supported; remote URLs use `wss`.
The URL is used unchanged, with Bearer auth and `kapy.jsonrpc.v1` subprotocol.

For real handler integrations in the machine container, import `MachineService`
from `kapy.execution.daemon`. Enter `ExecutionStore(paths, machine_id)` and an
`httpx2.AsyncClient(trust_env=False, follow_redirects=False)`, construct
`MachineService(store, http_client=client, child_env={...})`, then initialize it.
`await service.handle(method, params)` dispatches the frozen process/file/session
RPCs directly. Always `await service.aclose()` before closing client/store. This
is the actual daemon service, not an alternate test implementation. Session
ensure precedes starts; file.push/chunk/finish and ordinary chmod/shell argv are
sufficient to install and run stdin-consuming plugins without new RPC fields.

The internal `ProcessManager` admits at most 16 active processes. Stdio stdout
and stderr stream independently to complete private disk spools; reads return
at most 64 KiB per stream and do not retain whole outputs in memory. Stdio stdin
is closed; interactive input uses PTY mode. The real controlling PTY supports
write, window resizing and line-discipline Ctrl-C, retaining an 8192-byte tail.
PTY wait returns after 200 ms of quiet output; wait deadlines and `wait_ms=0`
only observe. A leader exiting does not truncate descendants' output or end a
still-live process group. Complete output does not claim containment of escaped
setsid/double-fork descendants.

Kill targets the ordinary process group. Only explicit kill, shutdown and
failure cleanup use a two-second drain bound; forced truncation is reported with
`output_complete=false` and an error. SQLite schema v2 stores process IDs,
startup digests and terminal output metadata, without raw argv/env/tokens.
Restart changes interrupted records to `lost` and never relaunches them; old
groups are killed only when the leader's Linux start identity still matches.
Released process IDs remain tombstones and cannot be reused.

Session release revokes credentials, rejects starts, kills its processes,
aborts transfers and deletes only its own managed directory. Interrupted release
is resumed before accepting requests. Child environments are explicit; the
daemon adds the four KAPY machine/session/socket identity fields and does not
inherit its environment. Session token refresh affects future children.

Domain operations survive a WebSocket closing. Unexpected disconnection retries
with 1–30 second jittered backoff; authentication/endpoint/protocol rejection is
fatal. Optional idle disconnect requires no active work or local connection,
reconnects after the configured interval, and can be woken by local proxy calls.
The proxy preserves caller auth separately from the target params and never
retries a call with uncertain effects. No cgroup or systemd configuration is used.

`resolve_paths(*, state_dir=None, data_dir=None, runtime_dir=None)` is the shared
CLI/daemon path calculation. It creates nothing. `ExecutionPaths.socket_path`
and `session_cwd(session_id)` provide the same socket name and UTF-8 SHA-256
session directory mapping to both callers. Explicit roots must be absolute.

The internal `ExecutionStore` owns a SQLite connection, state/runtime file locks,
and in-memory session credentials. Newly created application directories are
0700 and state files are 0600. Existing application roots must already be private
and owned by the current UID; startup does not silently change their permissions.
The database binds one machine ID, uses WAL, and retains released session IDs.
All blocking database and file operations share four worker slots. A canceled
operation finishes its current blocking I/O before the manager can close its FD.

Session ensure creates a stable cwd and refreshes the in-memory token. Restart
preserves cwd and durable records, but requires ensure before accepting new work.
The daemon coordinates resource shutdown before calling `finish_release`; that
method removes only the session's managed directory. Gateway owns the durable
cleanup obligation and retries `session.release` across machine outages. State
does not coordinate daemon cleanup. Files explicitly pushed outside the managed
cwd remain outside session directory cleanup.

The internal `FileManager` implements `file.push`, `file.pull`, `file.chunk`,
`file.finish` and `file.abort`, with the exact parameter/result shapes in the
approved Execution proposal. The daemon lends it an HTTP client configured with
`trust_env=False`, redirects disabled and TLS verification enabled. Call
`initialize()` before serving requests and `aclose()` before closing the store
and HTTP client. File operations belong to the manager, so losing the observing
RPC call does not cancel an accepted transfer.

WebSocket transfer chunks contain at most 65,536 decoded bytes. Push acknowledges
one offset at a time and accepts an identical retry of the last chunk. Pull uses
explicit byte cursors. A push stages in the target parent directory and commits
with fsync/replace through the same open parent FD. Existing destination data is
preserved on checksum failure, abort and incomplete download. Pull holds a
regular-file FD and fails if its identity, size or timestamps change; it does not
promise a snapshot of a concurrently modified source.

Presigned push downloads with GET; presigned pull uploads with PUT. Both stream
64 KiB chunks. GET requires identity content encoding and the declared size;
PUT sends the known Content-Length. HTTPS is required except for loopback HTTP
tests. Redirects are rejected, network I/O has a 60-second inactivity timeout,
and error response bodies are discarded with bounded reads. URL and header
values are hashed into the idempotency fingerprint and never stored in SQLite
or included in returned errors. The manager does not retry an upload whose
remote effect is uncertain.

At most eight transfers are active. Inactive transfers expire after ten minutes
(checked every thirty seconds). Restart marks interrupted transfers failed and
removes their staging paths; cross-daemon resumability is not provided. Completed
transfer IDs remain durable and do not restart on a repeated begin request.
WebSocket reconnection within a running daemon can continue using the existing
ID and cursor. A terminal URL failure requires a fresh transfer ID.

`call_local_proxy(socket_path, method, params, *, auth, timeout=60.0)` opens one
same-UID Unix connection, sends a JSON-RPC `proxy.call`, and closes the connection
on success, failure, timeout or cancellation. Each NDJSON line, including LF, is
bounded to 1 MiB while reading. `auth` is either
`{kind: "session", session_id, token}` or `{kind: "user", token}`. The caller's
`auth.session_id` and the target `params.session_id` remain independent. The
helper reads no environment or `.env`, and never retries an uncertain mutation.

Real execution tests run in a dedicated disposable Docker machine. From this
worktree, with the architect-provided `kapy-v2-machine:dev` image available:

```sh
docker run --rm --init --network none --memory 1g --pids-limit 128 \
  -e PYTHONPATH=/workspace/src -w /workspace \
  -v "$PWD/src:/workspace/src:ro" -v "$PWD/tests:/workspace/tests:ro" \
  -v "$PWD/pyproject.toml:/workspace/pyproject.toml:ro" \
  kapy-v2-machine:dev /app/.venv/bin/python -m pytest \
  -q -s -p no:cacheprovider tests/rpc tests/execution
```

The container has its own temporary XDG roots and loopback HTTP server; these
tests do not use or modify shared PostgreSQL/Valkey. The suite includes 64 MiB
WebSocket and URL transfers, hash comparison, peak RSS growth, real SQLite
restart after a process crash, cleanup failures and local socket framing.

The execution suite also covers 64 MiB stdio spool hashes, real PTY input and
Ctrl-C, delayed descendant output, 16-process limits, output recovery/tombstones,
environment isolation, actual WebSocket reconnection and local proxy forwarding.

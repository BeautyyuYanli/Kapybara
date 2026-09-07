# Kapy v2 implementation boundaries

`kapy_v2.md` is the product specification. This document assigns ownership and
sets the initial integration contract; seniors refine concrete signatures during
proposal review before implementation. The initial target is Linux, one control
server process, multiple independent sessions and execution machines. Durable
state survives control-server restarts. No clustering framework is required.

Read `docs/contracts.md` for architect decisions resolving older proposal conflicts.

## Ownership

| Senior | Packages | Responsibility |
| --- | --- | --- |
| Execution | `kapy.execution`, `kapy.rpc` | JSON-RPC 2.0 duplex peer, machine daemon, PTY/stdio, SQLite/XDG state, file transfer, local proxy endpoint, reconnect |
| State | `kapy.state` | PostgreSQL schema and history, safe session SQL, Valkey client, events, session lifecycle and runner coordination |
| Intelligence | `kapy.agent`, `kapy.skills` | Pydantic AI runner, tools and plugins, media fallback, compression, skill archive CRUD |
| Gateway | `kapy.gateway`, `kapy.cli`, `kapy.settings` | App composition, authenticated machine registry/control RPC, user API, CLI, Telegram frontend |

Seniors own their corresponding tests and module docs. The architect owns shared
project configuration, Docker environment, integration harness and product docs.
Subagents used by cmd-impl remain the senior's responsibility.

## Cross-module contracts to finalize in proposals

All external calls use JSON-RPC 2.0. WebSockets support requests in both directions
with bounded frames, concurrent request dispatch, ids, errors and disconnect cleanup.
The execution daemon makes the outbound connection to the control server. Local CLI
requests go through its local authenticated endpoint to the same control dispatcher.
Gateway owns machine authentication and association checks. Session tokens identify
the calling session separately from the machine, including recursive CLI calls.

Execution exports an async RpcPeer with `call(method, params)` and a request handler
callback. Gateway exports a machine caller for agent tools with
`call(machine_id, method, params)`; session_id travels in machine RPC params.
Execution methods use `process.*`, `file.*`, and `session.ensure`; control methods
use `session.*`, `event.*`, `history.*`, and `skill.*`. Concrete method names and
parameter shapes must be written down by both owners before implementation.

State exports SessionService. Gateway delegates session/input/output/event/history
operations to it. State owns per-session serialization, durable input buffers,
waiting-channel subscriptions, output cursors and state transitions. Intelligence
provides an injected runner callback: it receives a session run context, input and
history; emits deltas/messages through that context; polls steer input at model/tool
boundaries; and returns final output plus waiting ids. State alone transitions to
waiting and emits completion events. Final natural model completion also waits on
the session's own input channel. The detailed RunContext/RunResult must be agreed
between State and Intelligence. Output/history reads always scope by session.

PostgreSQL stores authoritative sessions, inputs, outputs/history and pending events.
Valkey provides wakeup hints/caching where useful; loss of a hint cannot lose an input
or strand an already committed event. History SQL is read-only and must enforce
session isolation even for joins/subqueries; do not rely on string concatenation or
an untrusted caller to add a WHERE clause. Read DB rows without ORM revalidation.

Skills live on the control plane with durable metadata/archive storage. Intelligence
exports their service; Gateway exposes CRUD and CLI transfer. Archives must have
bounded extraction, paths and metadata. Agent sessions load skill descriptions at
creation and can fetch newly added skills later. Agent tools run on the selected
machine, defaulting to the session default machine.

## Product acceptance

Acceptance covers process interactivity/timeouts/tree cleanup/8192-byte PTY buffers,
large stdio and chunked files, reconnect, session isolation and restart recovery,
steer/queue sequencing, broadcast/queued/self-excluded events, recursive completion,
history cursor replay and SQL isolation/search, compression and protocol-valid model
history, media rejection fallback, skills/plugins, CLI and Telegram chat/topic routing.
Use live PostgreSQL/Valkey plus real local subprocesses for integration. A small
live gpt-5.6-luna inference/tool test uses root .env; Telegram outbound calls are
tested against a fake Bot API until separately authorized. No unfinished stubs count
as delivery. Keep operational limits explicit and measurable.

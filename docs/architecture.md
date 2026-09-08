# Kapy v2 implementation boundaries

`kapy_v2.md` is the product specification. This document assigns ownership and
describes the implemented integration contract. The initial target is Linux, one control
server process, multiple independent sessions and execution machines. Durable
state survives control-server restarts. No clustering framework is required.

Read [contracts](contracts.md), including the [session interaction and output contract](contracts.md#session-interaction-and-output), for concrete public interfaces.

## Ownership

| Module | Packages | Responsibility |
| --- | --- | --- |
| Execution | `kapy.execution`, `kapy.rpc` | JSON-RPC 2.0 duplex peer, machine daemon, PTY/stdio, SQLite/XDG state, file transfer, local proxy endpoint, reconnect |
| State | `kapy.state` | PostgreSQL schema and history, safe session SQL, Valkey client, events, session lifecycle and runner coordination |
| Intelligence | `kapy.agent`, `kapy.skills` | Pydantic AI runner, tools and plugins, media fallback, compression, skill archive CRUD |
| Gateway | `kapy.gateway`, `kapy.cli`, `kapy.settings` | App composition, authenticated machine registry/control RPC, user API, CLI, Telegram frontend |

The lead owns integration and shared configuration. Domain boundaries describe resource
ownership rather than requiring a separate deployment or a separate contributor.

## Cross-module contracts

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
use `session.*`, `event.*`, `history.*`, and `skill.*`. Concrete method names and parameter shapes are documented in `docs/contracts.md`.

State exports SessionService. Gateway delegates session/input/output/event/history
operations to it. State owns per-session serialization, durable input buffers,
one-shot waiting-channel handoffs, output cursors and state transitions. Intelligence
provides an injected runner callback: it receives a State RunContext containing inputs and durable runner state; emits deltas/messages through that context; polls steer input at model/tool
boundaries; and returns a typed output plus its final checkpoint. Creation fixes output mode: text or
WaitFor, versus sequential reply_to with conditional completion or WaitFor. A reply_to call
atomically settles selected channels through RunContext.reply while the same loop continues.
Its tool return contains the complete ReplyTo and remaining unanswered addresses. The complete
tool batch ends the loop only after all consumed inputs are answered, or the model selects WaitFor.
State alone enters waiting; waiting itself does not complete inputs. Sessions have no default
channel or automatic self-listener. The shared RunContext/RunResult types are exported by State. Output/history reads always scope by session.

PostgreSQL stores authoritative sessions, inputs, outputs/history and one-shot result channels.
Valkey provides wakeup hints/caching where useful; loss of a hint cannot lose an input
or strand an already committed event. History SQL is read-only and must enforce
session isolation even for joins/subqueries; do not rely on string concatenation or
an untrusted caller to add a WHERE clause. Read DB rows without ORM revalidation.

Skills live on the control plane with durable metadata/archive storage. Intelligence
exports their service; Gateway exposes CRUD and CLI transfer. Archives must have
bounded extraction, paths and metadata. Agent sessions load skill descriptions at
creation and can fetch newly added skills later. Agent tools run on the selected
machine, defaulting to the session default machine.

## Configurable application boundaries

Runner borrows a `ModelBackend`, whose `create_model(name)` returns a Pydantic AI Model
and whose `classify_error(error)` returns a safe context-length/media failure or None.
Gateway-owned providers select OpenAI Responses (default), OpenAI Chat or Google AI Studio.
Each connection owns its endpoint/key; a persistent catalog keeps stable model IDs,
observed limits and manual defaults. Sessions retain model IDs and optional budgets.
Each Runner call freezes current provider/catalog values; no model credentials come
from service environment defaults. See [model resource contracts](models.md).

Frontend factories are selected by configured names. Each receives `FrontendContext`
with `ControlAPI`, settings, a borrowed metadata pool and schema. Only `ControlAPI.call`
is used for session/input/output/history operations. A trusted frontend authenticates
its own users and constructs `Principal("frontend", frontend_id=..., subject=...)`;
core authorization and recovery use its stable namespaced identity. Telegram keeps its
historical identity strings, owns its four tables, and cleans stale route/delivery rows
through session observations. Core migrations and deletion never require Telegram tables.

Runner registers only supplied ScriptTool definitions. Optional preparation receives a
narrow ScriptHost (workspace, successful stdio command, bounded file push), followed by
the ordinary script process path. Application composition provides apply_patch by default;
it can be disabled or replaced. No core tool dispatcher special-cases that plugin name.
The generated upstream resources are changed only by their generator.

## Product acceptance

Acceptance covers process interactivity/timeouts/tree cleanup/8192-byte PTY buffers,
large stdio and chunked files, reconnect, session isolation and restart recovery,
steer/queue sequencing, one-shot queued handoffs, reply selection and input-driven wakeups, recursive completion,
history cursor replay and SQL isolation/search, compression and protocol-valid model
history, media rejection fallback, skills/plugins, CLI and Telegram chat/topic routing.
Use real PostgreSQL/Valkey and non-root Docker subprocesses for integration. A small
live gpt-5.6-luna inference/tool test uses root .env; Telegram outbound calls are
tested against a fake Bot API until separately authorized. No unfinished stubs count
as delivery. Keep operational limits explicit and measurable.

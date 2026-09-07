# Integration decisions

The architect's decisions here resolve disagreements between earlier proposal
drafts. Module proposals and eventually exported source signatures contain the
full shapes. Seniors should implement these decisions without adding compatibility
aliases for discarded draft names.

## State and Runner

- Use State's UUID-based SessionSpec, SessionView, Submission, RecordPage,
  RunContext, RunnerState, CheckpointWrite, OutputDelta and RunResult.
- A SessionRunner is an async callable taking one RunContext and returning one
  RunResult. Intelligence imports these types rather than defining another protocol.
- request_id is a caller-supplied UUID, independent of the JSON-RPC envelope id.
  Create/input/publish and update/delete use durable idempotency. Telegram maps
  bot/update/action to a stable UUID. There is no RequestKey(scope, key) type.
- The additional State wait_submission(session_id, request_id, wait_seconds=0)
  method may observe durable receipts without consuming channel events. Snapshot
  export_history is also accepted. These reuse State data, not another broker.
- Gateway owns identity, parent/child grants, channel grants, request ownership
  and machine cleanup obligations. State receives already-authorized calls and
  list_sessions(session_ids=...) filters. State has no ACL/scope/parent framework.
- State owns and closes its pool and Valkey client. Gateway owns its metadata pool
  and can lend it to Intelligence's skills/media storage. App lifespan invokes
  each module's migrations; no centralized migration framework is needed.
- Output/history/query result pages are limited to 512 KiB of actual encoded JSON
  including page fields, leaving room within the 1 MiB RPC message limit.

## Execution and RPC

- RpcPeer is an async context manager with send_text, receive_text, close_transport
  and handler callbacks. MachineCaller.call(machine_id, method, params,
  *, timeout=60.0) is the shared machine-caller protocol.
- The HTTP codec export is async dispatch_json(payload: str,
  handler: RequestHandler) -> str | None. It reuses the peer's protocol validation;
  there is no need for a fake transport or another public handle_request alias.
- Machine process methods use process.start with mode=stdio|pty, process.wait,
  write, resize, kill, list and release. Immediate reading is process.wait with
  wait_ms=0 and max_bytes. There is no separate process.run or process.read RPC.
- File methods are file.push/pull/chunk/finish/abort. Transfers have stable UUIDs,
  64 KiB raw chunks and explicit completion/failure. Callers calculate pull hashes
  if needed. There are no file.stat/read/write compatibility RPCs.
- session.ensure receives session_id and session_token and returns session_id/cwd.
  session.release is the resource cleanup method. Session tokens bind both session
  and machine, and the caller session differs from the operation's target session.
- Local proxy uses Execution's call_local_proxy with ProxyAuth, NDJSON proxy.call,
  remote control.proxy, and KAPY_DAEMON_SOCKET. Gateway CLI owns argument parsing;
  Execution owns transport/framing. An Execution-owned XDG path resolver is allowed.
- User clarification: execution-machine research and acceptance run in a dedicated
  Docker container. Use ordinary process groups and best-effort descendant cleanup;
  perfect cleanup of escaped setsid/double-fork descendants is not required. There
  is no mandatory cgroup delegation or cgroup_root setting. Lody is only the agent
  coordination tool, not part of Kapy's execution environment.
- Any stdin capability needed by plugins must be agreed between Execution and
  Intelligence and remain bounded. Existing file transfer supports large content;
  executable permissions can be established by ordinary managed commands.

## Intelligence and Skills

- Intelligence owns durable media content, reference encoding and hydration.
  State checkpoints/history contain stable references, not large inline base64.
  Store content before the referencing checkpoint and use the saved content on
  recovery. Intelligence exports initialization and session-media cleanup for
  Gateway to compose. Model-attempt markers fit existing opaque output data.
- User clarification: token counts come from provider API usage. Do not calculate
  a second token count with tiktoken or byte-length heuristics. Preserve reported
  usage for compression decisions; before the first report the count is unknown.
- Intelligence exports an explicit session initialization function returning
  RunnerState with the creation-time skill-description snapshot. Gateway invokes it.
- wait_for authorization uses an injected Gateway capability; UUID knowledge is
  not itself permission. State alone commits waiting and emits completion.
- Skills use path-based upload/download through existing execution file transfers.
  Bounded archives and metadata live in PostgreSQL; do not add another chunk-upload
  handle subsystem. CLI can operate with an explicit or inherited session context.
- Skill archive pack/extract helpers are owned by Intelligence and reused by CLI.
  Gateway owns caller/creator authorization metadata. Services must not silently
  retry an unknown mutation as a fresh operation.

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
  Use the latest response's input_tokens + output_tokens, not cumulative RunUsage.
  Each fresh usage observation permits one sweep; stale usage cannot repeatedly
  degrade history. The latest approximately 10% may be selected by complete
  interaction blocks, without claiming that block counts measure tokens. Only an
  explicit context-length rejection permits bounded additional compression retries.
- Runner.initial_state returns RunnerState with the creation-time skill-description
  snapshot. Gateway invokes it.
- wait_for authorization uses an injected Gateway capability; UUID knowledge is
  not itself permission. State alone commits waiting and selected replies; waiting does not settle requests.
- Skills use path-based upload/download through existing execution file transfers.
  Bounded archives and metadata live in PostgreSQL; do not add another chunk-upload
  handle subsystem. CLI can operate with an explicit or inherited session context.
- Skill archive pack/extract helpers are owned by Intelligence and reused by CLI.
  Gateway owns caller/creator authorization metadata. Services must not silently
  retry an unknown mutation as a fresh operation.

### Approved Python exports

- Runner(config, machine_caller, *, model_backend, payload_store, authorize_wait,
  plugins=(), model_identity=None) borrows its dependencies; initial_state(*, instructions, skills)
  returns State.RunnerState. Its async call takes State.RunContext and returns
  State.RunResult. Construction has no I/O or background tasks.
- AuthorizeWait accepts a session UUID and tuple of channel UUIDs, returns None
  asynchronously on success, and raises PermissionError on rejection. Gateway
  supplies it; State remains responsible for one-shot waiting and result handoff.
- AgentPayloadStore(pool, *, schema="kapy_agent") borrows Gateway's metadata pool
  and exposes initialize, put(session_id, bytes), get(session_id, ref), and
  delete_session(session_id). Immutable bytes are keyed by session UUID and SHA-256.
  There is no foreign key to State's private tables or state_schema parameter.
  Gateway's durable cleanup calls delete_session after State has stopped the runner
  and deleted the session. Store bytes before committing references. A payload is
  at most 64 MiB; contexts above 2 MiB can use a stored reference.
- SkillService(pool, *, schema) borrows the same Gateway-owned pool and exposes
  initialize, create/update/delete, get, catalog, download. Mutations have a stable
  internal request_key scoped by Gateway; update/delete also use expected_revision.
  SkillDescription contains id and description. pack_skill(source_dir, archive_path)
  and extract_skill(archive_path, destination) are shared by CLI. ZIPs are at most
  16 MiB; transport reuses Execution's file operations.
- Skills/AgentPayloadStore have no separate resource factory, start/aclose or pool
  ownership. Gateway owns initialization and shutdown ordering.

## Configurable adapters (2026-09-08)

- `RunnerConfig` holds model/context/output/compression/media settings only.
  `ModelBackend.create_model(model_name) -> Model` and
  `classify_error(Exception) -> ModelFailure | None` isolate the adapter. The provided
  `OpenAICompatibleBackend(*, base_url, api_key, http_client)` borrows its HTTP client.
- `create_app(settings=None, *, model_backend_factory=create_model_backend, frontend_factories=None, plugins=None)`
  registers trusted Python frontend factories selected through Settings.frontends. Context
  contains Settings, ControlAPI, borrowed pool and schema; business calls use
  `ControlAPI.call(method, params, *, principal)`. Frontend.run owns plugin migration/tasks.
- `Principal("frontend", frontend_id=..., subject=...)` persists as `frontend_id:subject`.
  operator/session namespaces remain reserved; old Telegram identity strings are unchanged.
  Core migration/deletion no longer reference adapter-owned Telegram tables.
- `ScriptTool(..., prepare=None)` optionally accepts an async `(ScriptHost, ProcessCommand)`
  hook. Host.workspace/run/push wrap existing durable bounded machine operations; no Runtime
  is exposed. Runner adds no plugins implicitly. Gateway's default configured plugin is
  apply_patch, with explicit sequence or configuration allowing removal/replacement.
- Model tools do not include file_read/file_write. Text inspection/editing uses process
  commands and optional patch tools. Raw execution file RPCs and read_media are unchanged.


## Provider resources and model selection (2026-09-08)

Provider owns type (Responses default, Chat or Google AI Studio), endpoint and current key.
The stable model catalog stores observed metadata and separate user defaults; discovery never
replaces defaults. Session config.model accepts model_id and optional token budget overrides only.
Effective budgets are session > model defaults > observed > 262144/16384, subject to known limits.
Each run freezes the current provider/catalog; provider/default changes affect later calls.
Provider deletion clears the key and leaves session history. Credentials never enter State config
or generic request JSON. CRUD/defaults/discovery and their receipts commit atomically in Gateway.
Full signatures, authorization and lifecycle are documented in [models](models.md).

## Session interaction and output

Creation config accepts `output_mode: "text" | "reply_to"`, default `text`; the value is immutable.
Text mode exposes `wait_for(ids)` and natural text completion. Reply mode exposes
`wait_for(ids)` as an output function and `reply_to(ids)` as a sequential function tool.
Partial replies continue the same loop; a full tool batch with no unanswered consumed inputs
can end with its last committed ReplyTo. Normal mode does not expose
reply addresses or its protocol instructions. The CLI accepts `session create --output-mode`.
Create/input no longer accept `waiting_id` or `--waiting-id`; State generates one address for
each direct input, and idempotent retries reuse it. Empty creation returns `submission: null`.

State exports `WaitFor(waiting_ids: tuple[UUID, ...])`,
`ReplyTo(being_waited_ids: tuple[UUID, ...], payload: str)` and
`SessionOutput = str | WaitFor | ReplyTo`. `RunResult(output: SessionOutput,
checkpoint: CheckpointWrite)` contains the framework output itself. The model's reply schema
contains only `ids`; its function fills payload with the latest complete visible model
text. Persistence, history compression and waiting handoff retain the complete output DTO.
No downstream path substitutes the payload field for output.

`SessionInput.being_waited_id: UUID | None` identifies a directly submitted input's reply channel;
waiting-result inputs leave it null. `RunContext.unreplied_addresses(*, after=0, limit=64)` returns
`ReplyAddressPage(being_waited_ids, next_after)` for consumed unresolved inputs in input order.
A checkpoint acknowledges consumption, while a reply settles its channel.
`RunContext.reply(*, emission_id: UUID, output: ReplyTo,
validate_receipt: Callable[[ReplyResult], None] | None = None) -> ReplyResult` commits while running.
The optional internal callback synchronously validates the caller's complete receipt envelope
before publication, including on idempotent replay. Runner uses the same real message encoder
and MessageWrite validation as persistence, then saves that exact prepared message. State
validates its own history and channel envelopes. No callback or payload parameter is model-visible.
`ReplyResult(output: ReplyTo, remaining_being_waited_ids: tuple[UUID, ...])` is its complete tool
return. The internal emission ID is derived from the persisted model response and tool call;
replay returns the original receipt before validating current address eligibility.
Text settles all consumed unresolved inputs. A reply call settles only its selected IDs and
preserves active waits; a final text or ReplyTo clears active waits.
WaitFor accepts 1–128 unique IDs, settles nothing and replaces active waits. ReplyTo accepts up
to 128 unique IDs; `[]` requires no read unanswered inputs. Partial replies leave remaining
obligations in the same loop. The tool returns remaining addresses and the model continues;
unread queue inputs do not prevent completion. New steer requires fresh model text.
WaitFor can pause a loop with unanswered inputs. Already committed replies survive later
failure, deletion and recovery. Finishing with ReplyTo validates the last reply record and an
empty unresolved set without republishing any result. Cycles retain every complete output
in order, with the final ReplyTo present only once.

Channels are one-shot and one-to-one: `open → ready → delivered`. Results published before
waiting remain ready. The sole durable handoff inserts an input with `event_id=channel_id`.
Producer/receiver endpoints cannot be rebound, even after clearing an active wait. There is
no default session channel, self-subscription, self-producer exclusion, broadcast, or second
logical publication. Each selected reply address receives the complete output in a
`{"type":"waiting","waiting_id":...,"producer_session_id":...,"output":...,"outcome":...}`
envelope. Independent external events use the same one-shot handoff. Direct input reply
channels reject ordinary `event.publish` calls.

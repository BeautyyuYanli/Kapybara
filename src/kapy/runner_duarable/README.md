# Temporal Agent runner

`RunnerWorkflow.run(RunnerInput)` restores the supplied message history, runs
`agent.run()`, records messages through `kapy.record_history`, saves the final
state through `kapy.save_runner_state`, then returns its string output. Callers
serialize executions for each session; do not run the legacy and Temporal writers
for the same session. This runner does not acquire a lease. Callers resolve model
configuration, merge settings, and read state and version together before starting:

```python
runner_state, version = await repository.read_runner_state(session_id)
RunnerInput(
    session_id=session_id,  # An existing session UUID.
    runner_state_version=version,
    runner_state=runner_state,  # Required; None for a new session.
    user_prompt="Hello",
    config=DurableExecutionConfig(
        provider_class="pydantic_ai.providers.openai:OpenAIProvider",
        model_class="pydantic_ai.models.openai:OpenAIResponsesModel",
        model_name="gpt-4o-mini",
        api_key="...",
        model_settings={"temperature": 0.2},
    ),
)
```

`SessionRepository.read_runner_state(id)` returns `(opaque_state, version)` from
the same row; a missing session raises `LookupError`. New sessions start at
`(None, 0)`. The Workflow fixes both values in its input. It decodes non-null state
using `ModelMessagesTypeAdapter.validate_json()` and passes the message list to
`agent.run(message_history=...)`. Invalid JSON or message structure raises SDK
`UserError` and fails the Workflow; it never silently starts over. After success,
`result.all_messages_json().decode("utf-8")` saves the complete accumulated history,
including metadata seq marks and the unrecorded final tail. State remains a bare SDK
message-array JSON string, without a wrapper or cumulative run usage. The session
layer stores it opaquely and does not expose it through session DTOs/HTTP.

`SessionRepository.save_runner_state(id, expected_version=..., runner_state=..., layer_states=...)`
borrows a transaction and locks the session row. Matching the current version
replaces the message snapshot and all layer States together, increments the
version once, and updates `updated_at`. The session must be READY and the State
names must match its configured layers. If the current version is already
`expected_version + 1` and both messages and States match exactly, the save
succeeds without changes. Any other version/content mismatch raises
`ValueError`. Two concurrent distinct results from the same base version cannot
both commit. The same base version, messages and States count as one submission, regardless
of Workflow identity; retrying never refreshes the base version. Configuration
updates and lifecycle changes do not increment this version.

The save Activity owns and commits its database transaction before returning. It
has a 30-second start-to-close timeout and uses Temporal's default retry policy
for transient failures. Missing sessions and state conflicts become non-retryable
`ApplicationError`s with types `SessionNotFound`, `RunnerStateConflict`, and
`SessionLifecycle`. Save
failure propagates to the Workflow. Replay uses Temporal's recorded completion;
if a commit succeeded but its acknowledgment was lost, repository idempotency
handles the repeated Activity. Agent failure leaves the previous message snapshot intact.
Cancellation or timeout during saving cannot undo an already committed transaction,
so an unsuccessful Workflow may still have saved state. Saving never changes the
session's lifecycle status. Until the final save commits, failure/cancellation
leaves both messages and layer States at the preceding successful snapshot.

## Layer configuration and State

`CreateSession.layers` accepts registered names and configuration objects, such as
`{"shell": {"cwd": "/workspace", "base_url": "http://shellctl:8765"}}`. Creation
validates Config and default States, commits creating with fixed identity/Config under
the session lease, then calls optional `enter_scope`, which may allocate business
resources and returns the initial State, including required fields. Without enter,
State initializes from `{}`. Complete State is published atomically with ready.
Only ready can start a Workflow or change execution configuration. Abandoned creation
exits every configured layer with None rather than replaying enter; exit must clean
by scope ID/Config and be idempotent. Enter only permits retries via an explicit
RetryableScopeError. See the [scope lifecycle contract](../layers/README.md#business-scopes).
Names/configuration stay fixed for the session. The public session DTO returns
configuration; internal PostgreSQL `sessions.layer_states` holds execution State.
`SessionService.start_durable_runner()` reads both JSON columns with the message
snapshot/version under its admission lock and fixes `RunnerInput.layers`.

The installed [MCPs layer](../layers/README.md#mcps-layer) accepts inline upstream
configuration and credentials, sharing a run-owned stdio Gateway across native
discovery and tool Activities. Session creation and Workflow replay do not launch
MCP clients. It has empty State and no scope hooks.

## MCP Gateway run ownership

When `mcps` is enabled, the Workflow starts `kapy.hold_mcp_gateway` on its task
queue plus `-mcp`, then waits for its readiness signal before entering the Agent.
The owner keeps a native stdio MCP client entered, holding the same Gateway
process across discovery and multiple calls. It exposes a temporary HTTP relay
for SDK Activities, which may run on other Workers. Those short-lived clients
close only their HTTP sessions. A random run bearer protects the relay and is
not forwarded upstream; the stdio Gateway still requires no JWE or authentication.

The Workflow cancels and waits for the owner in `finally` after `agent.run` ends,
before saving the final snapshot. This covers success, error and cancellation.
The owner heartbeats during startup and execution; every ten seconds it also
checks the exact Workflow run's status to reclaim resources after termination.
Each owner attempt has a one-day start-to-close timeout, a ten-second heartbeat
timeout and at most three attempts. An owner Worker shutdown closes its sockets
and pipe, then reports a retryable failure unless the Workflow requested cancellation.

Main Worker restarts do not interrupt an owner on another Worker. If the owner
fails or restarts, Temporal creates a new owner/Gateway and readiness signals
update the descriptor for subsequent calls. Late signals from older attempts are
ignored. Successful recorded calls are not replayed; Gateway process memory is
ephemeral and is not restored. An in-flight tool call still follows the existing
single-attempt policy: losing its connection can fail the run without repeating
an ambiguous side effect. This lifetime applies to the Gateway; its upstream
connections remain request-scoped as described in the aggregation contract.

Only the URL, bearer and attempt travel in `RunnerDeps.mcp_gateway`, never live
clients or processes. The descriptor is not saved in PostgreSQL configuration,
Layer State or messages; Temporal records it as execution payload. Replay consumes
recorded readiness/completions without starting an owner or connecting to a relay.
The Workflow patch `kapy-mcp-run-gateway-v1` preserves the previous Activity-local
path when replaying older MCP histories without this marker.

Owners bind an ephemeral TCP port on the Worker's network interface. All main
Workers must be able to reach it. By default the advertised host is the owner's
hostname IPv4 address; `KAPY_MCP_RUN_HOST` overrides it. Compose forwards this
setting to `runner-worker`; its default network needs no extra published ports.
The relay is separate from Kapy's public HTTP interface.

Custom deployments register `hold_mcp_gateway` on a companion Worker polling
`<runner task queue>-mcp`; it needs a Pydantic-AI-configured Temporal Client but
no database or output service. It can live in the main Worker process or a separate
process/host. Keep the normal AgentPlugin and persistence Activities on the main
queue, and bind the output service for those main Workers as below.

## Layer State transport

The [layer factory contract](../layers/README.md) assembles static typed `LAYERS`
with explicit model types, native capabilities and business hooks. The Agent
installs a static layer selector and selects enabled definitions from
current deps using Kapy's injected `get_layers` rule. Each known capability leaf
has a native Dynamic Activity entry named by its stable capability ID; Combined
composition and ownership remain in the Workflow. Tools read their own entry
through factory-injected access and explicitly replace State JSON. Workflow and
Activity each bind their process-local static capabilities through the SDK;
stateful SDK components use `for_run` isolation. Factories and business hooks do
not run on reconstruction or replay. Temporal serializes only deps data, without
Layer, hook or access-function instances; State transport performs no database I/O.

On ordinary tool success, an inner native Toolset wrapper validates the called
layer's State using its captured Layer model type and injected access rule, then
carries it in SDK `ToolReturn.metadata`. The outer Workflow
wrapper applies only that layer's State before business hooks, then restores the
original ordinary value or native ToolReturn, including its content, metadata
and tools. Transport metadata never reaches model messages. Other layers'
pending Workflow changes survive. Temporal records the successful result and
State together, so replay needs neither database nor shell I/O for this boundary.

Activity errors, cancellation and native `ModelRetry` follow the SDK's existing
protocol and return no local State. Remote shell operations already performed are
not rolled back or compensated. Even successful tool States stay only in the
running Workflow until `snapshot_layers()` joins the final message transaction;
a later run failure leaves the previous database inventory intact. Session close
can clean only jobs in that saved inventory.

All layer tools execute sequentially, so the next Activity receives the preceding
call's returned State, including calls to different leaves of the same layer.
Tool ownership can be a leaf ID while State belongs to its enclosing layer ID.
Runner obtains Activity entry IDs through the Toolset tree's public traversal,
then applies the same policy to each entry. The leaf Activities have a finite shell hard
timeout plus 60 seconds, a 10-second heartbeat timeout, at most one attempt, and
wait for cancellation cleanup. Non-idempotent shell operations are not retried
by Temporal. SDK `ModelRetry` still asks the model for a new call through its
normal protocol. A lost remote acknowledgement can mean an operation executed.

Shell jobs survive a completed run. Exclusive session close calls every configured
Layer's optional `exit_scope` in reverse configuration order with its latest State,
then validates and saves State
after each hook attempt. Shell removes confirmed jobs sequentially within an attempt;
the database save occurs at the attempt boundary. Cleanup failure retains CLOSING
and saved layer progress for a close retry.
New layer sessions reject legacy `plugins` and the old Python runner entry;
existing legacy plugin sessions retain their original execution and StateStore.

Drain old Workflows before deploying this Agent definition. The successful tool
payload and State transport changed; replay compatibility with previous Workflow
code is not provided.

## Message recording and realtime output

The shared Agent injects one stateless `Recorder` into `MessageRecordCapability`
and the innermost `Hooks(before_model_request=recorder.predict_response_seq)`.
The before hook only computes `RunnerDeps.response_seq` from `ctx.messages` and
returns the same request context. It performs no recording, repair, metadata
changes or history replacement. Model Activities inherit this prediction and
retries reuse it; later requests overwrite it. SDK instruction synchronization
and provider preparation continue on the separately prepared model-facing list.

The recording capability orders outside other capabilities, so reverse post-hook
execution records last in `after_node_run` for each `ModelRequestNode`. Business
post-hooks can finish the response before its request/response batch is recorded.
A model request failing before this boundary records no new history. There is no
`after_run` recording: final output-tool returns and business after-run additions
remain only in `runner_state` until a later model node records the surviving tail.
Ordinary text final responses are already recorded at their model node.

SDK `metadata.seq` marks successful recording. Present values must be strictly
increasing nonnegative strict integers; numeric gaps are allowed. All messages
after the last mark form the unrecorded tail, starting at that position + 1 (or
zero without a mark). The recorder deep-copies this tail, assigns seq, and creates
`HistoryMessage(session_id, seq, message)` values. One `kapy.record_history`
Activity commits and publishes the batch. Only after acknowledgment does the
recorder mark live SDK messages; failure leaves them unmarked. An empty tail
schedules no Activity. The recorder has no run-local numbering counter, database
maximum-seq query, model wrapper or capability lookup.

Business hooks preserve recorded content and seq. New content belongs in an
unrecorded tail. Do not insert or reorder working-history messages between
prediction and post-node recording: that would invalidate delta `response_seq`.
This contract does not reconcile instruction merging with SDK history or copy the
model-facing prepared list back into working history. To reproduce an input,
select recorded entries through its request, excluding the generated response;
restore in seq order with the SDK codec, use the same instructions, tools,
settings and SDK version, and let the SDK prepare them again without a new prompt.

`RunnerActivities.record_messages(MessageBatch)` keeps the Activity name
`kapy.record_history` and a 30-second timeout. Its transaction locks the session
row with `SELECT id FOR UPDATE` before `AgentRepository.append_history()`.
Other direct durable writers hold the same lock through commit. Different sessions
can write concurrently, and no lock spans publication. Missing sessions become
non-retryable `SessionNotFound` errors.

Every complete history message is immutable. The repository uses the stored
maximum seq before the batch (-1 for empty history) as one fixed boundary. It
inserts only entries above it, preserving their input order; all old positions and
numeric gaps at/below the boundary remain sealed. There is no overwrite, promotion
or provisional-row cleanup. Invalid seq, duplicate positions and cross-session
entries reject the batch with non-retryable `InvalidHistory` before any writes.
DTO positions come from columns; JSON metadata is payload, not a storage decision.
Normalized token storage and the official SDK codec remain unchanged.

The Activity publishes only inserted input DTOs after commit, retaining complete
SDK usage details without a database reconstruction. `MessageBatch` uses
`extra="ignore"` for provider-specific nested SDK usage fields. Retried stored
positions are discarded and do not publish again. A commit followed by a crash or
transport failure can miss a broadcast, recovered by history polling or gap
backfill. No outbox or exactly-once contract is added. Completed Activity replay
performs no database/output I/O; raw broadcasts can arrive late or out of order.

History batches and runner-state saving remain separate transactions. A failed
run can leave recorded history while the previous successful state remains.
Storage seals history but does not reconcile stale state. Callers serialize runs
and do not restart failed executions from an older successful snapshot.
`SessionService.start_durable_runner()` enforces this with a short session lock and
fixed `kapy-runner:{session_id}` Workflow ID. It admits a new execution only
initially or after COMPLETED, and rejects business plugin bindings and legacy
checkpoints. Queues, cancel flags and compaction settings remain unused.

`SessionService.live(after_seq=-1)` subscribes before reading history, then combines
complete broadcasts, ordered backfill on numeric jumps, and periodic reads for
missed commits. All complete messages advance the connection cursor on output;
the client persists its cursor only after fully applying them. Deltas advance
neither cursor and never trigger history reads. Gaps are valid and complete
messages are sorted and deduplicated by seq. Reconnection clears temporary
previews and resumes from the last fully applied complete message.
After applying a complete message, consumers discard previews at/below its seq
and keep later previews. Direct `AgentOutputService.subscribe()` remains available
for consumers owning their replay policy; its buffering is described in
[the output contract](../agent_runner/README.md). Telegram sends text responses as
fixed formal replies, persists one pending send and chunk offset, and advances
its cursor when that message is fully sent. It has no editable reply receipts.

The module defines its own Agent at module scope. It reuses Provider/Model
construction helpers, but does not import the legacy application Agent factory.

Module passthrough is centralized in the package exports: shared model helpers
transitively import SQLModel definitions that cannot be reinitialized in the
sandbox. Model class resolution also passes imports through because dynamically
loading Google SDK dependencies there otherwise triggers restricted environment
access. The Workflow also passes static registry and owner definitions through;
sandboxing remains enabled and their resource operations execute only in Activities.

Class references select installed trusted code, just as existing model
configuration does. The DTO contains the actual API key: Temporal persists it in
Workflow and Activity payloads. `repr=False` only suppresses repr output; payload
protection and access control belong to the deployment.

`python -m kapy.runner_duarable.worker` starts the main Worker for Workflow and
SDK Activity tasks and its companion MCP owner Worker. It uses `PydanticAIPlugin()`
on their Client and `AgentPlugin(agent)` on the main Worker. These are SDK registration plugins, not Kapy
business plugins. SIGTERM and SIGINT shut down both Workers; main Activities
receive up to 15 seconds to finish before cancellation. The owner Worker then
retires its long-lived Activities immediately so their pipes close and other
Workers can retry ownership.

The Worker owns PostgreSQL and Valkey clients through `open_resources()`. It
creates one `AgentOutputService` with prefix `settings.valkey_namespace +
":agent-output"`, injects it into `RunnerActivities`, and binds it with
`bind_output_service(outputs)` before starting Worker tasks.
Register `record_messages` and `save_runner_state` alongside
`AgentPlugin(agent)`; custom
Workers must also establish the binding around their entire Worker lifetime.

`RunnerActivityContext` borrows this service in its constructor. SDK default
context deserialization and `dataclasses.replace()` copies both call this
constructor, so all asynchronous Activities on the Worker loop share the resource.
The service is excluded from SDK serialization and never placed in deps, state,
or Workflow inputs. Module passthrough keeps the context binding's identity shared;
Workflow code never reads the binding or uses network clients. The same module-level
Agent registers the context type and `handle_deltas` for Workflow and AgentPlugin.

The SDK owns the model Activity stream. `handle_deltas` consumes it completely and
maps text/thinking events through the same `to_text_delta` function as the legacy
OutputCapability. Tool-only streams are drained without text output. Each invocation
owns a publisher; successful completion flushes its tail before Activity return.
Worker shutdown finishes Activities before unbinding and closing Valkey/database
resources. Publishers own only their buffers and tasks; the output service borrows
the client and needs no separate close operation.

The Worker and interface processes share `KAPY_TEMPORAL_ADDRESS` (default
`localhost:7233`), `KAPY_TEMPORAL_NAMESPACE` (`default`), and
`KAPY_TEMPORAL_TASK_QUEUE` (`kapy-runner`). Applications create one Temporal Client
per `open_resources()` lifespan, available as `resources.temporal_client`; HTTP
also exposes it through `request.app.state.resources.temporal_client`. Startup
requires a reachable Temporal service. Client has no explicit close API; process
owners finish their tasks before releasing resources. HTTP direct runner submissions and ordinary Telegram text use this Workflow.
Queue submissions do not start it, and legacy Python start_runner remains available.

The resolver reads configuration from `RunnerDeps.config` and uses the existing
Provider/Model builders to construct the real SDK
model and closes these initial contexts before returning it. Workflow code uses
its protocol-specific profile and request preparation. The SDK subsequently
re-enters and closes model contexts on both the Workflow and Activity sides;
providers recreate their owned HTTP clients on re-entry. The Workflow-side
context remains open while waiting for the model Activity, but HTTP requests
execute only inside Activities. This relies on the Provider re-entry contract:
custom configured providers must support it, and
constructors/profile preparation must not perform external I/O. No requests are
made from Workflow code. Client construction also occurs during replay; pin SDK
and model/provider code consistently with in-flight Workflow definitions.

Workflow-side resolution uses the actual SDK model without duplicating protocol
behavior, allocating and closing clients during resolution. The protocol tests
cover OpenAI Chat, OpenAI Responses, and Google SSE, including sandbox execution,
Activity cleanup, realtime snapshots/deltas, and replay without repeating HTTP
requests or broadcasts.

`docker compose up -d runner-worker` starts the Worker and pinned auto-setup server.
It creates `temporal` and `temporal_visibility` databases on the existing
PostgreSQL instance, including on an already initialized volume. Kapy's database
and schema migrations are separate. The host endpoint is
`127.0.0.1:${KAPY_TEMPORAL_PORT:-7233}`, and the Compose network endpoint is
`temporal:7233`.

```sh
docker compose build runtime
docker compose up -d temporal
docker compose run --rm --no-deps -e KAPY_TEMPORAL_ADDRESS=temporal:7233 \
  runtime python -m pytest -q -p no:cacheprovider tests/runner_duarable
```

Model Activities have a five-minute start-to-close timeout and allow at most one
retry (two attempts total). The SDK's default heartbeat timeout and non-retryable
error classification still apply; no schedule-to-close timeout is set. History
recording and state saving retain their separate timeout and retry policies.
External requests can repeat after an unrecorded completion; no exactly-once
guarantee is added. Workflow failures propagate to callers.

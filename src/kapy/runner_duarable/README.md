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

`SessionRepository.save_runner_state(id, expected_version=..., runner_state=...)`
borrows a transaction and locks the session row. Matching the current version
replaces the state, increments the version once, and updates `updated_at`. If the
current version is already `expected_version + 1` and the string matches exactly,
the save succeeds without changes. Any other version/string mismatch raises
`ValueError`. Two concurrent distinct results from the same base version cannot
both commit. The same base version and string count as one submission, regardless
of Workflow identity; retrying never refreshes the base version. Configuration
updates and lifecycle changes do not increment this version.

The save Activity owns and commits its database transaction before returning. It
has a 30-second start-to-close timeout and uses Temporal's default retry policy
for transient failures. Missing sessions and state conflicts become non-retryable
`ApplicationError`s with types `SessionNotFound` and `RunnerStateConflict`. Save
failure propagates to the Workflow. Replay uses Temporal's recorded completion;
if a commit succeeded but its acknowledgment was lost, repository idempotency
handles the repeated Activity. Agent failure leaves the previous snapshot intact.
Cancellation or timeout during saving cannot undo an already committed transaction,
so an unsuccessful Workflow may still have saved state. Saving never changes the
session's lifecycle status. Partial failure/cancel snapshots are outside this
runner's contract.

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
access. The Workflow module itself uses normal imports; sandboxing remains enabled.

Class references select installed trusted code, just as existing model
configuration does. The DTO contains the actual API key: Temporal persists it in
Workflow and Activity payloads. `repr=False` only suppresses repr output; payload
protection and access control belong to the deployment.

`python -m kapy.runner_duarable.worker` starts the independent Worker for both
Workflow and Activity tasks. It uses `PydanticAIPlugin()` on its Client and
`AgentPlugin(agent)` on its Worker. These are SDK registration plugins, not Kapy
business plugins. SIGTERM and SIGINT shut down the Worker; running Activities
receive up to 15 seconds to finish before cancellation.

The Worker owns PostgreSQL and Valkey clients through `open_resources()`. It
creates one `AgentOutputService` with prefix `settings.valkey_namespace +
":agent-output"`, injects it into `RunnerActivities`, and binds it with
`bind_output_service()` before starting Worker tasks. Register both
`record_messages` and `save_runner_state` alongside `AgentPlugin(agent)`; custom
Workers must also establish the binding around their entire Worker lifetime.

`RunnerActivityContext` borrows this service in its constructor. SDK default
context deserialization and `dataclasses.replace()` copies both call this
constructor, so all asynchronous Activities on the Worker loop share the resource.
The service is excluded from SDK serialization and is never placed in deps, state,
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

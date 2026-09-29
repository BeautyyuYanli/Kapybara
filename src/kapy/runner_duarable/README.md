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
    # next_seq=100,  # Optional explicit start for the first new batch only.
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
including metadata seq marks and the final message. State remains a bare SDK
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

## History recording

`HistoryRecordCapability` is registered on the shared Agent, with a fresh instance
per run. It wraps all other capabilities so its `after_node_run` observes their
final mutations. After every `ModelRequestNode`, it scans `ctx.messages` for the
last `metadata["seq"]` and records the entire unnumbered suffix. `after_run` records
any remaining suffix (including output-tool return messages) and overwrites the
last `ModelRequest` at its existing seq, capturing its final content. A Request
already in the new suffix is included only once. Other numbered messages are not
compared for edits or automatically rewritten.

Each message has one nonnegative integer seq; other metadata keys survive marking.
Retained marks must increase in history order, but gaps are allowed. Numbering is:

- With no explicit start, continue after the last retained seq.
- `RunnerDeps.next_seq` holds the caller's optional first-batch start. It must be
  greater than the last retained mark. It stays unchanged; only the run-local
  first-record flag changes after successful persistence. Later batches derive
  numbering from history, never from a separate counter or database `MAX(seq)`.
- An initially empty history can start at 0 before its first recorded batch.
  Otherwise, no retained marks means an unused explicit `next_seq` is required.
  Restored unnumbered tails are ordinary pending messages.

SDK initialization can merge requests and drop their metadata. There is no
`before_model_request` repair hook: if a retained seq 9 precedes old requests
10/11 merged without marks, the merged message is recorded as 10, and subsequent
messages start at 11. Existing rows may be overwritten. seq identifies a database
row, not a permanent identity across history transforms. Business capabilities
must keep retained marks ordered and keep all messages needing incremental
recording after the last mark. Rebuilding messages must preserve metadata when
original numbering is needed. Losing all marks after a batch is an error, even
for a run that originally started empty or supplied an explicit start.

The capability makes independent copies, assigns their seq values, and awaits
`kapy.record_history` before marking the live messages. The Activity commits a
single transaction through `AgentRepository.upsert_history()`; failure leaves
new live messages unmarked. `RecordHistoryInput` carries the session UUID and SDK
messages with metadata seq, using the SDK Pydantic Temporal converter. Invalid or
duplicate batch seq values fail as non-retryable `InvalidHistory`; transient
failures use Temporal's default retries and a 30-second start-to-close timeout.
Replay applies marks again using recorded Activity completions.

The existing `agent_history` primary key `(session_id, seq)` uses upsert semantics.
Same-seq content changes are legal. An overwrite replaces kind, parts, metadata,
finish reason and normalized token columns, clearing obsolete nullable values;
`created_at` stays unchanged. No checkpoint, lease, state or input queue is touched
by this Activity. There is no schema migration, conflict-content comparison or
revision arbitration: callers serialize session runs, and commit order wins.

History batches and the final runner-state save are separate transactions. A
failed run may leave recorded history while the previous state remains available.
Rerunning from that state can overwrite rows, and uncovered rows are not deleted.
History therefore is not a mirror of the last successful snapshot. `after_seq`
queries do not return updates at an already-consumed seq. This runner does not
publish `MessageCommitted` events or implement realtime update delivery.

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

The Worker uses the same `open_resources()` factory as the interfaces, owning
its own PostgreSQL pool, Valkey client, and Temporal Client. Its resource scope
encloses the Worker so Activities finish before resource cleanup. Database and
Valkey connections are lazy; these process-local objects must not enter Workflow
inputs or state. The entry point constructs `RunnerStateActivities` with
`resources.core_session_factory` and registers both `record_history` and
`save_runner_state` alongside `AgentPlugin(agent)`. Custom Workers must register
both bound methods as well.

The Worker and interface processes share `KAPY_TEMPORAL_ADDRESS` (default
`localhost:7233`), `KAPY_TEMPORAL_NAMESPACE` (`default`), and
`KAPY_TEMPORAL_TASK_QUEUE` (`kapy-runner`). Applications create one Temporal Client
per `open_resources()` lifespan, available as `resources.temporal_client`; HTTP
also exposes it through `request.app.state.resources.temporal_client`. Startup
requires a reachable Temporal service. Client has no explicit close API; process
owners finish their tasks before releasing resources. Session execution is not
automatically switched to this Workflow.

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

Compared with the proposal's lazy descriptor, this uses the actual SDK model
without duplicating protocol behavior. It allocates and closes clients during
Workflow-side resolution, rather than prohibiting client construction entirely.
The protocol tests cover OpenAI Chat, OpenAI Responses, and Google, including
sandbox execution, Activity cleanup, and replay without repeating HTTP requests.

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

Temporal owns model Activity timeout/retry behavior through SDK defaults. External
requests can repeat after an unrecorded completion; no exactly-once guarantee is
added. Workflow failures propagate to callers.

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
including metadata seq/authoritative marks and the final message. State remains a bare SDK
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

`MessageRecordCapability` is a stateless capability on the shared Agent. Three
boundaries record the entire suffix after the last authoritative SDK message:

| Boundary | authoritative | Ordering |
| --- | --- | --- |
| Before the model request | False | Innermost `Hooks(model_request=...)`, directly outside Temporal dispatch |
| After each ModelRequestNode | True | Last `after_node_run`, after business hooks finalize messages |
| After the run | False | Last `after_run`, including any output-tool return tail |

The recorder wraps all capabilities so reverse post-hook execution makes it last.
Register business wrappers, including other innermost peers, before the request
hook. This hook is later than SDK `before_model_request`: instructions and business
request preparation have finished before it records and predicts the response.

Only messages with `metadata.authoritative=True` anchor numbering. Their
`metadata.seq` values must be nonnegative strict integers and strictly increasing;
gaps are allowed. Missing authority means False; invalid flag/seq types fail.
Every message after the last authoritative one gets numbered again, starting at
that seq + 1 (or zero without an anchor). Non-authoritative seq values may repeat
or be stale; they never advance the anchor. There is no caller-supplied start,
run-local counter, or database maximum-seq query.

The recorder deep-copies this suffix, assigns seq and authoritative in SDK
metadata, and creates `HistoryMessage(session_id, seq, authoritative, message)`
from the same values. One `kapy.record_history` Activity commits the entire batch,
then broadcasts `MessageCommitted` snapshots from those exact input DTOs. Only
after acknowledgment does the recorder mark live SDK messages. Failure never
prematurely marks them. Empty suffixes do not schedule an Activity. The request
hook sets `RunnerDeps.response_seq` to the next position while invoking the model
handler, then clears it in `finally`. Model Activities inherit that prediction;
retries reuse it.

Business hooks must preserve the authoritative prefix. Once the node's messages
are authoritative, later hooks may only change non-authoritative tails or append
new messages. There is no final Request rewrite. During a model request, do not
insert or reorder messages before the response: that would invalidate the predicted
seq used by provisional deltas. Content changes are allowed. Node-after recording
always uses actual SDK history; no cross-seq preview migration is provided.

`RunnerActivities.record_messages(MessageBatch)` retains the registered Activity
name `kapy.record_history` and a 30-second timeout. Its transaction calls
`AgentRepository.upsert_history(session_id, entries)` with explicit DTO fields.
The `agent_history.authoritative` column sits alongside seq; the generated migration
initializes existing rows to False. JSON is opaque payload for storage decisions:
seq and authority are never extracted from JSON or reconciled with SDK metadata.
History DTOs take both attributes from columns. Existing SDK payload encoding and
normalized token columns remain unchanged; full events retain all input SDK fields,
including usage details, without a database reconstruction.

Upsert unconditionally replaces `(session_id, seq)`, including authoritative and
nullable fields, while preserving created_at. Invalid seq/authority, mismatched
session IDs, and duplicate positions within a batch fail as non-retryable
`InvalidHistory`. Authority is a producer convention, not a conflict condition.
Transient failures retry normally: repeated writes and broadcasts are allowed,
and full output events replace the same key regardless of authority or seq order.
Only a committed batch is published. Transport failures do not roll back history;
commit followed by a crash can miss broadcasts. No outbox or exactly-once contract
is added. Replay of completed Activities performs no storage or output I/O.

History batches and the final runner-state save remain separate transactions. A
failed run may leave recorded history while the previous state remains available.
Rerunning from that state can overwrite rows, and uncovered rows are not deleted.
The successful state includes seq/authority metadata, including a non-authoritative
after-run tail that can be re-numbered in the next run. History is not a mirror of
the last successful state. `after_seq` queries cannot recover earlier overwrites.

Use `AgentOutputService.subscribe()` for this output path. A full snapshot replaces
the same key and clears its provisional text; later deltas form a fresh preview,
never append into the complete message itself. `SessionService.live()` assumes
contiguous append-only history and is not this runner's consumer; HTTP and Telegram
execution are not switched to Temporal here. Transport buffering and close behavior
are described in [the runner output contract](../agent_runner/README.md).

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

Temporal owns model Activity timeout/retry behavior through SDK defaults. External
requests can repeat after an unrecorded completion; no exactly-once guarantee is
added. Workflow failures propagate to callers.

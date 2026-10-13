# Standalone Activity runner and session scopes

`RunnerActivity.run(activity_id)` executes the complete module-level static Agent
inside one Temporal standalone Activity. `create` and `close` are thin standalone
Activities for the session's existing business scopes. The Worker registers all
three. There are no Workflows, Temporal capabilities, model/tool sub-Activities,
checkpoints or Agent recovery.

## Ownership and execution

The Worker borrows database and output services for its lifespan. Each invocation
atomically claims its pending operation before external I/O, verifies the native
`activity_id` and `activity_run_id`, and loads its frozen input from PostgreSQL.
Temporal payloads contain only the UUID. Private operation input contains provider
credentials; neither the public operation DTO nor session DTO exposes it.

The static Agent and LayerRegistry keep no model clients or mutable session State.
Each run owns its provider/model contexts, writable `RunnerDeps`, native per-run
and tool resources, and heartbeat task. The actual Model and merged settings go
directly to `agent.run`. Model/provider shutdown is bounded and cancellation-safe.
The Worker retains ownership until resources have exited and the final transaction
has committed. Every new model request, tool call and business scope hook checks
its current operation and fixed deadline first. Already-issued remote I/O cannot
be revoked by database fencing.

Shell tools run sequentially and share the run-local job inventory/cursors.
Native ModelRetry keeps local State mutations if the run eventually succeeds.
Remote jobs belong to the session business scope, survive SDK client shutdown and
are released by explicit session close. A failed run can leave remote jobs outside
the saved inventory; the current shellctl ownership API cannot discover them.

## Admission and failures

Each create/run/close request supplies a UUID `activity_id`. The same UUID identifies
the permanent database operation and native Temporal Activity. Identical intent
reuses the first frozen input or terminal result; changed intent conflicts. New
operations use new IDs. Temporal uses `USE_EXISTING` for running executions and
`REJECT_DUPLICATE` for retained terminal records. The database ledger keeps the
business key after Temporal retention or deletion.

A run claims READY → STARTING in a short database transaction. The Worker claims
pending → running and STARTING → RUNNING before any model work. No transaction or
explicit row lock spans a Temporal RPC, model/tool request or scope hook. The
operation's current ID, allowed session status, active phase and deadline fence
all progress writes. The former lease and runner snapshot version are removed.
See [control state machine](../control/README.md#session-state-machine).

Every Activity has one Temporal attempt, a fixed one-hour operation deadline and
a ten-second heartbeat timeout. Heartbeats run every two seconds without stored
checkpoint details. Same-key dispatch confirmation does not extend the deadline.
The pre-existing business-hook retry contract remains: only an outer
`RetryableScopeError` permits up to three local attempts; this does not retry a
Temporal Activity or resume the Agent.

Caller cancellation leaves the remote operation running. A lost start response
keeps ownership and permits same-key confirmation. Failed/cancelled runs become
CLOSING; only explicit cleanup with a new key can continue. Operation queries,
same-key submissions and competing admissions reconcile confirmed terminal
executions or seal expired operations. Unknown RPC results and NOT_FOUND never
release ownership within the deadline. There is no background recovery loop.

## Storage and output

Runner state is the latest successful bare SDK message-array JSON string. Each
model node predicts the response seq before dispatch, then commits complete
message copies under conditional ownership. Publication follows commit with no
open database transaction. Old positions and gaps stay sealed; live history
polling recovers missed broadcasts. SDK messages receive metadata.seq only after
recording/publication succeeds. Deltas never advance replay cursors.

Final output-tool receipts and after-run tails first enter the final snapshot;
a later model node records them. After successful execution and resource exit,
messages, all validated layer States, READY and the operation's string output
commit together. Failure before this commit retains the preceding snapshot while
already committed history and remote effects remain. Database success is
independent of Temporal result acknowledgment: a lost commit/result acknowledgment
cannot overwrite succeeded or trigger creation compensation. A terminal operation
is never submitted again.

## Worker and infrastructure

`python -m kapy.runner.worker` registers create/run/close on the configured queue.
SIGTERM drains Activities for fifteen seconds; database and output clients close
only after Worker tasks exit. HTTP/Telegram create and close wait for saved business
results; run submission returns its durable operation without waiting for model
completion. Interface shutdown does not cancel Worker calls.

Compose pins Temporal Server/admin-tools 1.32.1 and uses SDK >=1.33.0 with the
native Pydantic converter. Schema/namespace initialization is repeatable against
retained volumes. Tests use real PostgreSQL, Valkey, native standalone Activities,
local deterministic model protocols and a private shellctl service.

# Standalone Activity Agent runner

`RunnerActivity.run(RunnerInput)` executes the complete module-level static Agent
inside one Temporal standalone Activity. It validates the supplied message
snapshot and layer data, constructs a provider/model, calls ordinary `agent.run`,
records each completed model node and saves the final snapshot before returning
its string output. There are no Kapy Workflows, Agent Temporal capabilities,
model/tool sub-Activities, replay logic or checkpoint recovery.

## Ownership and execution

The Worker borrows a database session factory and output service for its lifespan.
Each invocation has its own `RunnerDeps`, layer data, provider/model contexts and
heartbeat task. Deps contain borrowed resource references and never cross a
Temporal serialization boundary. The Agent and LayerRegistry are static process
objects without model clients or mutable session State. Native `for_run` hooks
and per-tool contexts own their normal SDK resource lifetimes.

`RunnerInput` contains only session UUID, prompt, fixed `ExecutionConfig`,
message snapshot/version and JSON layer Config/State. Configuration includes the
API key and is stored in Temporal payloads; hiding it in `repr` does not encrypt
it. Callers use Temporal's native `pydantic_data_converter`. The Activity passes
the actual Model instance and merged settings directly to `agent.run`; provider
and model exit are bounded and protected from cancellation by existing runtime
helpers. `handle_deltas` uses the ordinary SDK event stream handler and each
publisher closes after flushing its temporary preview.

Kapy uses ordinary `LayerRegistry({"shell": shell.make}, get_layers=get_layers)`.
Tools mutate the current deps directly, with no ToolReturn metadata envelope.
Shell tools explicitly declare `sequential=True`, so multiple calls share updated
job inventories and byte cursors safely. Native ModelRetry retains any earlier
successful State mutations in that run; only successful Agent completion saves
those mutations to PostgreSQL. Remote jobs belong to the session business scope,
survive tool/client close and are cleaned by explicit session close.

## Admission and failures

`SessionService.start_runner(session_id, user_prompt="...")` holds the session row
lock while checking readiness and Temporal status, reading the snapshot/version
and submitting `Client.start_activity`. It returns native `ActivityHandle[str]`.
The fixed Activity ID is `kapy-runner:{session_id}`; the returned `id` and `run_id`
identify the confirmed execution. A running or paused Activity is busy. Only an
initial run or the preceding COMPLETED run admits another prompt. Other terminal
statuses raise RunnerConflict, avoiding divergent history from an older snapshot.
RPCs have a ten-second budget. NOT_FOUND also includes an expired or deleted
terminal record, so admission allows it only with no snapshot, version zero and
no stored history. Otherwise a new session is required, including after an expired
successful record whose completion can no longer be confirmed. An uncertain
submission is never automatically repeated. Local request cancellation does not cancel the
accepted Activity. Session close uses the same row lock and rejects active runs.
The separate session lease protects native layer scope ownership, not execution.

Admission configures `RetryPolicy(maximum_attempts=1)`, one-hour schedule-to-close
and start-to-close limits and a ten-second heartbeat timeout. Heartbeats run every
two seconds throughout execution and saving, without checkpoint details. Worker
loss, timeout, failure or cancellation ends that run; there is no Agent retry or
resumption policy. Normal model SDK and native tool behavior remain in effect.
Already committed history and remote effects cannot be rolled back. In particular,
a failed shell run can leave remote jobs outside the saved inventory; close cannot
discover those jobs through shellctl's current ownership interface.

## Storage and output

Runner state remains a bare SDK message-array JSON string. Invalid JSON or message
shape fails before model I/O. Each model node predicts the response sequence before
request dispatch, then the outer recording capability commits complete message
copies after the node finishes. It locks the session row only for this short
transaction and publishes accepted full messages after commit. Old positions and
numeric gaps are sealed permanently; a missed broadcast is recovered by
SessionService.live history polling. Live SDK messages acquire `metadata.seq`
only after recording/publishing succeeds. Deltas do not advance replay cursors.

Unrecorded final output-tool receipts and after-run tails enter the final snapshot
and are recorded by a later model node. After Agent success, the same Activity
validates all layer States and uses `SessionRepository.save_runner_state` in one
transaction to replace messages and States and increment the fixed version once.
The session must be READY and State names must match its configured layers. An
identical replacement at `expected_version + 1` is idempotent; any other mismatch
fails. Failure before that commit leaves the preceding successful snapshot.
There is no separate save Activity or storage retry.

## Worker and infrastructure

`python -m kapy.runner.worker` registers only `RunnerActivity.run` on the configured
queue. SIGTERM drains Activities for fifteen seconds; clients close after the
Worker exits. HTTP and Telegram processes only submit and observe executions.
Compose uses pinned Temporal Server/admin-tools 1.32.1 and requires SDK >=1.33.0.
A one-shot schema service creates/updates the two existing Temporal PostgreSQL
databases, then the server starts with `config/temporal/server.yaml`. Namespace
initialization waits for RPC health; Worker and interfaces depend on its success.
The schema and namespace setup can run again against retained volumes.

Tests use real PostgreSQL, Valkey, standalone Temporal Activities, local HTTP model
protocols and a private shellctl service. They cover single-attempt failure,
resource cleanup, cancellation, admission races, final save failure, history
recording, successive prompts and sequential shell State. See [tests](../../../tests/README.md).

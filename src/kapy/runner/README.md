# Standalone Activities and conversations

The Worker registers one standalone Activity, `kapy.run_agent`. Each invocation
runs the complete Agent graph within the Activity.

## Ownership and requests

Submission uses USE_EXISTING and REJECT_DUPLICATE. A retained terminal rejection
provides its native run_id; clients pin all later reads/waits/cancellation to that
execution. Same-key prompt changes use the first accepted input. Only
session/type identity is validated against Temporal. Deduplication and result
retrieval end with retention; there is no permanent Kapy execution ledger.

The Activity payload contains `session_id` and optional `user_prompt`. Session
creation and closure use the PostgreSQL lifecycle described in
[control services](../control/README.md#session-state-machine).

Run executes an UPDATE on an unowned ready row, storing actual activity.info()
UUID and activity_run_id. The same short transaction copies current model/provider,
validates settings/history/State and constructs process-local RunnerInput. Failure
rolls back admission. Queueing neither reserves the session nor orders execution;
a competing Worker can fail admission after HTTP 202, without model/tool/hook I/O.
Provider credentials, snapshot and State never enter run's Temporal parameters.
Every actual `agent.run` explicitly receives `conversation_id=str(session_id)`.
The first, later and restored runs all use that host identity; historical message
IDs remain unchanged. Tools and SDK hooks read `ctx.conversation_id`; the SDK run
ID and Temporal Activity identity remain independent execution identifiers.

Every history and snapshot write checks session ID, both native identity fields
and ready status. The Worker checks ownership before opening runtime handles
and before model and tool calls. RPC, hooks, model requests and publication run
outside database transactions.

Run admission and close share an exact Temporal lookup for an occupied session.
Confirmed terminal status or `NOT_FOUND` permits a conditional update matching
both observed identity fields. Live executions and RPC failures preserve the
writer. If input validation fails during admission, PostgreSQL rolls back the claim.

## Resources, cancellation and results

Each Activity owns model/provider clients, local Layer handles, writable deps
and a heartbeat task. The Worker opens Layer handles before constructing the
provider and model. The SDK manages the native Toolsets, including one inline
stdio Gateway per selected MCPs run. Shell exit closes the local HTTP client,
and shellctl manages remote jobs and log retention.

Each Activity has one Temporal attempt, one-hour schedule-to-close/start-to-close
budgets and ten-second heartbeat timeout. Heartbeats run every two seconds with
no checkpoint details. There is no database deadline or local timeout sealing.
Caller cancellation only stops waiting. Actual SDK cancellation stops new calls
and permits bounded local cleanup. The writer identity fences later progress
after a takeover.

After all runtime handles exit, run commits the latest SDK messages and validated
States and releases the writer atomically. Failure retains the last saved
node-boundary snapshot, States and committed history, and attempts to release the
writer in a shielded transaction bounded to 15 seconds. The Session stays ready.
A lost acknowledgment can leave a committed snapshot alongside a Temporal
failure or timeout.

Complete messages are recorded after each model node under conditional ownership,
then published after commit. Positions/gaps stay sealed. Output-tool receipts and
after-run tails first enter runner_state and are recorded at a later model node.
See [history](../agent_history/README.md) and [control](../control/README.md).

`RunnerPersistenceCapability` runs after inner node hooks and saves messages and
validated layer States atomically for these transitions:

| Transition | Intermediate snapshot |
| --- | --- |
| model → tool | Working history plus a new request containing the interruption retry prompt below |
| model → model | Working history plus the next model node's pending request, including its retry prompt |
| tool → model | Working history plus the next model node's pending request, including tool returns or retry feedback |
| tool → end | Keep the preceding snapshot |

The model → tool request contains an unbound `RetryPromptPart` with this content:

```text
Unexpected interruption. Your messages may or may not be delivered. If your messages were intended to make changes, retry only if the operation is idempotent; otherwise, verify the outcome before taking further action.
```

Every intermediate snapshot ends in a detached `ModelRequest(state="interrupted")`.
Live messages and immutable history are unchanged. No transaction or snapshot is
added for tool → end. Successful Activity completion still saves the final SDK
messages and States, including output-tool receipts and after-run tails, after
model/provider/tool resources close.

Submit a new Activity with `user_prompt=None` to resume the saved interrupted
request. The SDK inserts `ToolReturnPart(outcome="interrupted")` for unanswered
calls and sends the retained interruption feedback to the model. With a new
prompt, the SDK includes that input in the same model request. Actual tool
returns and tool-bound retry feedback are preserved. A prompt-free run with
empty history fails as `InvalidParameters`. If the history ends in a final
response, a prompt-free run returns its saved output without a model request.

If final output processing reached tool → end but the final Activity save did not
commit, resumption uses the preceding snapshot and asks the model again. An output
function that already ran may therefore run again on new output; its result and
the graph's `End` are not in the intermediate snapshot. Resumption uses saved layer
State while graph-local retry counters start fresh. The caller submits the next
Activity explicitly.

## Worker and verification

python -m kapy.runner.worker borrows database/output/Temporal resources for its
lifespan. SIGTERM drains Activities for fifteen seconds before resources close.
HTTP/Telegram create and close return committed session records through the
service. Run submission returns an execution reference. Tests use real
PostgreSQL/Valkey/Temporal, deterministic SDK models,
local model-protocol HTTP servers and an optional private shellctl service.
Compose pins Server/admin-tools 1.32.1; the locked Python SDK is 1.33.0.

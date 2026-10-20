# Standalone Activities and session scopes

The Worker registers kapy.create_session, kapy.run_agent and kapy.close_session.
A complete Agent run is one standalone Activity; there are no Workflows, model/tool
sub-Activities, checkpoints or internal Agent recovery.

## Ownership and requests

Submission uses USE_EXISTING and REJECT_DUPLICATE. A retained terminal rejection
provides its native run_id; clients pin all later reads/waits/cancellation to that
execution. Same-key prompt/config changes use the first accepted input. Only
session/type identity is validated against Temporal. Deduplication and result
retrieval end with retention; there is no permanent Kapy execution ledger.

Native inputs are deliberately small:

| Activity | Request |
| --- | --- |
| create | session_id and original CreateSession, including fixed layer Config |
| run | session_id and user_prompt |
| close | session_id |

Create defaults session_id to its Activity UUID. It validates all Config/default
States before inserting creating plus the native writer; duplicate session IDs
never repeat enter. Initial States remain local until published atomically with
ready and ownership release. Failure conditionally changes its still-owned creating
row to closing, then compensates all configured layers in reverse with None.
A lost ready-commit response cannot trigger compensation after ownership cleared;
an unavailable database does not authorize speculative cleanup.

Run executes an UPDATE on an unowned ready row, storing actual activity.info()
UUID and activity_run_id. The same short transaction copies current model/provider,
validates settings/history/State and constructs process-local RunnerInput. Failure
rolls back admission. Queueing neither reserves the session nor orders execution;
a competing Worker can fail admission after HTTP 202, without model/tool/hook I/O.
Provider credentials, snapshot and State never enter run's Temporal parameters.

Every history/snapshot/scope-progress write checks session ID, both native identity
fields and allowed lifecycle. Checks also precede new model/tool/hook calls. No
RPC, hook, model request or publication holds a transaction. Normal completion or
failed cleanup clears both fields; they represent write authority only.

Only explicit close queries an occupied session's exact old execution outside its
transaction. Confirmed terminal or NOT_FOUND permits CAS replacement into closing;
RUNNING/PAUSED/unknown states or temporary RPC failure preserve the old writer.
A changed writer cannot be overwritten. Later explicit close uses a new UUID and
may repeat idempotent exits. Each attempt saves validated State, including failure
and cancellation; ordinary failures still attempt other layers. Only outer
RetryableScopeError permits three local hook attempts, separate from Temporal retry.

## Resources, cancellation and results

Each Activity owns model/provider clients, writable deps, SDK tool resources and
heartbeat task. The static Agent/registry retain no clients or mutable State. MCPs
uses one inline stdio Gateway/native MCPToolset per Agent run, closed by the SDK.
Shell remote jobs belong to the session scope and survive client shutdown until
close. Jobs missing from the saved shellctl inventory remain undiscoverable.

Each Activity has one Temporal attempt, one-hour schedule-to-close/start-to-close
budgets and ten-second heartbeat timeout. Heartbeats run every two seconds with
no checkpoint details. There is no database deadline or local timeout sealing.
Caller cancellation only stops waiting. Actual SDK cancellation stops new calls
and permits bounded shielded local cleanup/creation compensation. Replacement of
a writer prevents later writes but cannot revoke already-issued external I/O.

After model/provider/tool exit, run commits latest SDK messages, all validated
States, ready and ownership release atomically. Failure preserves the preceding
snapshot and committed history, then releases to closing. Business commit and
Temporal outcome are separate facts: a lost commit/result response can leave
committed business state alongside native failure or timeout. No saved result
can override native status or reconstruct a forgotten execution.

Complete messages are recorded after each model node under conditional ownership,
then published after commit. Positions/gaps stay sealed. Output-tool receipts and
after-run tails first enter runner_state and are recorded at a later model node.
See [history](../agent_history/README.md) and [control](../control/README.md).

## Worker and verification

python -m kapy.runner.worker borrows database/output/Temporal resources for its
lifespan. SIGTERM drains Activities for fifteen seconds before resources close.
HTTP/Telegram create and close await native results; run returns an execution
reference. Tests use real PostgreSQL/Valkey/Temporal, deterministic SDK models,
local model-protocol HTTP servers and an optional private shellctl service.
Compose pins Server/admin-tools 1.32.1; the locked Python SDK is 1.33.0.

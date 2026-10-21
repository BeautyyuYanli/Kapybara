# Standalone Activities and session checkpoints

The Worker registers kapy.create_session, kapy.run_agent, kapy.resume_agent and
kapy.close_session. Each execution is one standalone Activity with one Temporal
attempt. Explicit resume starts a new execution from the latest consistent
business checkpoint; there are no Workflows, internal step Activities, execution
ledger or automatic recovery process.

## Ownership and requests

Submission uses USE_EXISTING and REJECT_DUPLICATE. A retained terminal rejection
provides its native run_id; clients pin later reads/waits/cancellation to that
execution. Same-key prompt/config changes use the first accepted input. Only
session/type identity is checked against Temporal. Deduplication and old result
retrieval end with retention.

| Activity | Native request |
| --- | --- |
| create | session_id and original CreateSession, including fixed layer Config |
| run | session_id and user_prompt |
| resume | session_id |
| close | session_id |

Every call supplies an Activity UUID. Only an executing Worker claims the database
writer, using its actual Activity UUID and native run_id. Queueing reserves no
business state and provides no ordering guarantee. Model/provider credentials,
RunnerInput, State and messages never enter run/resume Temporal parameters.

Create validates Config/default States before reserving creating and entering
scopes outside transactions. Complete initial State, ready and ownership release
commit together. Failure retains ownership during reverse compensation with None,
including uncalled/partially entered layers. A lost success-commit reply cannot
authorize compensation after ownership was cleared.

Ordinary run admits only an unowned ready session whose runner_state is NULL,
compatible legacy messages or a valid completed checkpoint. Its admission
transaction validates fixed history against the stored messages and copies current
provider/model/settings and validated layer State into a process-local RunnerInput.
Invalid or unfinished snapshots roll back admission before any runtime resources.

Every progress write uses a conditional UPDATE with the complete native identity
and permitted lifecycle. Each actual business callback checks ownership,
cancellation and the Recorder's stopped state before entry, including successive
Hooks registrations, marked event listeners and error/recovery callbacks. A failed
gate or failed/uncertain save stops further business callbacks and graph work;
ordinary SDK error recovery remains available while owned. Native resource
finally/exit cleanup remains permitted. No transaction spans callback or other I/O.
Fencing prevents future calls/writes; already-issued external I/O cannot be revoked.

The innermost before_output_process hook checks again after awaited output
preparation, immediately before native TextOutput/ToolOutput functions execute.
Explicit ordering places Runner's final hooks after other capabilities, including
business hooks that also request innermost. Conflicting ordering constraints are
rejected by native capability sorting.
Wrapper handler re-entry repeats that hook; native argument validation, conversion
and error handling remain with the SDK.

The runtime adapter in [ownership.py](ownership.py) copies the Agent's capability
tree, hook registrations and native output validators for each execution. Each
validator checks the gate immediately before delegating to native invocation,
retaining context signatures, sync/async execution, transformations and retries.
The static Agent/registry retains no run deps, State or clients. The internal
OwnershipCapability supplies its own operation gates and bypasses generic
business-callback wrapping. SDK WrapperCapability/tree traversal preserve native
ordering and rebinding. SDK 2.54.0 lacks public root-replacement and individual
registration visitors, so the localized compatibility adapter also uses its root,
Hooks dispatch, marked-event descriptors and OutputValidator; verify these when
upgrading the SDK.

## Atomic checkpoints

Recorder owns an execution-local frozen prefix and a confirmed numeric boundary.
All four boundaries use Recorder.persist_messages and the existing append_history
in one short guarded transaction, storing complete messages, runner_state and all
layer States together. The first full writer guard locks the row before history
reads/appends; snapshot/State assignment and final ownership release reuse that
same row, without another conditional UPDATE/refresh:

| Boundary | Saved next action |
| --- | --- |
| Before the first model dispatch, after request preparation/hooks | model_request |
| After a successful model node and its hooks | call_tools, or a model retry request |
| After a complete successful tool batch and its hooks | model_request, or finish with candidate output/tool identity |
| After after_run and every SDK/model/provider resource exit | done with final string output; release writer atomically |

runner_state uses only version=1, checkpoint, messages and next. It retains native
SDK fields and full message usage. History normalizes usage using its existing
codec; consistency compares that stored projection rather than raw JSON bytes.
Every saved message has a strictly increasing nonnegative metadata.seq. The
maximum seq, or -1 for empty messages, is the sealed history boundary; numeric gaps
remain closed. Prediction uses this same boundary; deltas allocate no positions.

A returned ModelRequestNode.request is not yet in SDK working history. Recording
includes its copy and acknowledges seq on the original only after commit; the SDK
appends it exactly once at the next dispatch. Empty tails still save timing and
State. A partially failed model/tool node never becomes a completed checkpoint.

capture_run_messages() exposes the SDK's actual working list after UserPromptNode
rebinds it. Complete stored records remain separate from SDK-normalized sending
views. Request preparation may refresh timestamp/instructions and fill unset SDK
identity fields; it cannot rewrite frozen messages. Business hooks may append
messages, but edits/removal/reordering of published parts, metadata and positions
fail before graph advancement. Final after_run tails are frozen at done.

Only acknowledged commits mark live SDK seq and advance the Recorder. Accepted
messages broadcast afterward. Ordinary message/preview transport errors do not
undo saves or interrupt the model stream; live polling repairs missed messages.
Cancellation still exits, retaining an already committed checkpoint. An uncertain
commit reply stops execution without saving-confirmation retry or repeated calls.

## Explicit resume

SessionService.resume_runner(session_id, activity_id=new_uuid) submits the same
timeout/heartbeat/reuse policy as run. The Worker first requires ready scopes. An
occupied session is checked against that exact old Activity UUID/native run_id
outside the transaction. Only terminal or NOT_FOUND permits a CAS takeover.
RUNNING/PAUSED/unknown states and RPC failures retain the old writer. A changed
writer/lifecycle makes the CAS fail. Normal run, reads and edits do not reconcile
abandoned ownership.

After claiming the row, the same transaction reads the latest checkpoint and
validates every sealed history position and its content projection. A done
checkpoint releases the new writer and returns its saved output without loading
execution configuration, layers, hooks, model/provider or tool resources.

Unfinished checkpoints load fixed Config and saved State, with current model,
provider credentials, settings and context window. Deleted/incompatible
configuration is not revived. No enter_scope is repeated. Shell resumes saved
scope_id/jobs/offsets; MCP State remains {} and each new SDK run owns a fresh
Gateway/toolset.

The run uses user_prompt=None and a native ResumeCapability after SDK
initialization, selecting the saved action explicitly:

- model_request reuses the complete saved request, preserving old identity/seq.
- call_tools handles the saved response, even when instructions are present.
  All pending tool names/arguments must validate against current definitions
  before any tool in that batch executes.
- finish returns the saved FinalResult directly to SDK after_run and resource
  teardown, without repeating model/tools/output validation.
- done returns during admission and never enters the SDK.

SDK run identity, usage/retry counters, per-run caches and execution stacks start
fresh. Old messages keep their native identities and usage. Hooks needing input
context read saved messages; ctx.prompt is not guaranteed to be the original input.
Unsupported custom nodes and malformed continuations are rejected.

## Failure, close and native results

After local resources exit, failure cleanup reads the actual committed state under
the complete writer guard. Start with an unfinished checkpoint releases to ready;
start without a new checkpoint releases to closing. Resume failure keeps the
actual checkpoint/State and releases to ready even before its first new save.
Cleanup only changes lifecycle/writer; it never writes partial local messages or
State. Database failure leaves the outcome unconfirmed. A committed done already
cleared ownership and cannot be downgraded by old failure cleanup.

Ready with an unfinished checkpoint permits only explicit resume or close.
New prompts and execution/title edits conflict. Close claims closing before
exit_scope, preserving its existing incremental cleanup and retry behavior;
closing/closed never resume the Agent. Their layer_states represent cleanup
progress, while runner_state/history remain the final Agent checkpoint.

Tools completed before their batch checkpoint may replay after failure. Partial
after_run side effects and failed resource exits may also replay from finish.
Tools/hooks must be reentrant or use business idempotency; there is no exactly-once
external side-effect guarantee. Unrecorded shell jobs may remain undiscoverable.

Business done and each native execution's outcome are independent. get_activity
always reports Temporal's status, including failure/cancellation/timeout after a
successful business commit. A resume returning done creates a new native result;
it neither repairs an old result nor creates permanent Activity-UUID history.

Legacy arrays can only supply context for a new prompt. Their numbered prefix
must match all existing history; only an unnumbered tail can be appended beyond
the old maximum. History ahead of the snapshot, interior unnumbered messages,
mismatched positions/content or NULL with history are rejected. Legacy arrays
never become fabricated done checkpoints and cannot explicitly resume.

## Worker and verification

python -m kapy.runner.worker borrows database/output/Temporal resources for its
lifespan. SIGTERM drains Activities for fifteen seconds before resources close.
Each Activity has one-hour schedule/start-to-close budgets, ten-second heartbeat
timeout and heartbeats every two seconds. Caller cancellation only stops waiting;
native cancellation stops new work and permits bounded shielded local cleanup.

Tests use real PostgreSQL/Valkey/Temporal, deterministic SDK models, local model
protocol HTTP servers and an optional private shellctl service. Compose pins
Server/admin-tools 1.32.1; the locked Python SDK is 1.33.0. See
[tests](../../../tests/README.md), [history](../agent_history/README.md) and
[control](../control/README.md).

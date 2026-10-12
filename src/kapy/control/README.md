# Control services

`ModelService` manages provider/model configuration. `SessionService` manages
session configuration and native layer scopes, admits durable execution through
Temporal, and joins immutable history with live output. The independent Worker
owns the Agent, model and tool resources. Durable is the only supported runner.

Applications own engines, session factories, Valkey and Temporal clients. Services
borrow them and never migrate schemas implicitly. Initialize with `kapy db upgrade`;
see [database migrations](../database/README.md). Core references are checked by
services without physical foreign keys; history retention is independent of
configuration and lifecycle.

```python
from kapy.application.resources import open_resources
from kapy.application.sessions import create_session_service
from kapy.application.settings import CommonSettings
from kapy.control.sessions import CreateSession


async def run(settings: CommonSettings, provider_id, model_name: str):
    async with open_resources(settings) as resources:
        sessions = create_session_service(resources, settings)
        session = await sessions.create_session(
            CreateSession(provider_id=provider_id, model_name=model_name)
        )
        handle = await sessions.start_durable_runner(session.id, user_prompt="Hello")
        return await handle.result()  # Requires an independent runner-worker.
```

## Execution admission and state

`start_durable_runner(id, user_prompt="...")` accepts a string prompt and returns
`WorkflowHandle[RunnerWorkflow, str]` after Temporal confirms the start. The fixed
Workflow ID is `kapy-runner:{id}`. `is_durable_runner_running(id)` observes Temporal;
only NOT_FOUND means absent, and RPC failures propagate. RPCs have a ten-second
budget. Session leases represent scope ownership, not runner status.

Admission holds a short session row lock through readiness/configuration checks,
Workflow status, the atomic state/version read and start confirmation. Model work
runs outside this transaction. Only an initial execution or a preceding COMPLETED
execution admits a new run. Busy raises SessionBusy; other terminal statuses raise
DurableRunnerConflict because starting an older successful snapshot could conflict
with already recorded history. An ambiguous start error never retries automatically;
query the fixed Workflow ID before resubmitting. Worker recovery continues the
original Workflow.

Provider values and merged model/session settings become DurableExecutionConfig.
The interface validates SDK settings without opening model resources. Native layer
Config and State are read from the locked row alongside the snapshot/version.
The Worker saves messages and every layer State together using a versioned,
idempotent replacement. New sessions have `(runner_state=None, version=0)`.
These internal values are absent from SessionRecord and HTTP. See the
[durable runner](../runner_duarable/README.md) for the complete save contract.

## Configuration

Providers have UUID identities and immutable importable Provider/Model class
references. OpenAIChatModel, OpenAIResponsesModel and GoogleModel are supported,
including compatible subclasses. Stored api_key, optional base_url and JSON
provider_kwargs go directly to the SDK constructor. Class references select trusted
installed code; client injection parameters are rejected. ProviderRecord returns
kwargs unchanged and excludes api_key. Credentials remain in the database.

Models use `(provider_id, model_name)` identity with a display name, request settings
and nullable context_window. Google names strip `models/`; later operations use the
returned name. Creation infers omitted/None capacity from the SDK profile without
making a model request. Unknown capacity remains None; setting it to None later
clears it without inference. Durable execution needs no compaction threshold.

`discover_models(provider_id)` imports missing models across SDK pages atomically,
preserving existing records and timestamps. Google discovery includes only
generateContent models. Unsupported listing/remote failure raises ModelDiscoveryError.
Get/update raises LookupError for missing configuration; deletion is idempotent.
ModelAlreadyExists identifies duplicate model identity. Session model references
may dangle: provider deletion removes its local models without rewriting sessions
or deleting remote resources. Execution requires the referenced model/provider.

Sessions expose UUID, title, model identity/settings, fixed native `layers` Config,
lifecycle status and timestamps. Unknown DTO fields are rejected. Updates preserve
omitted values and replace supplied JSON objects as a whole; `{}` clears an override.
Session fields cannot be explicitly null. Model switches supply both provider_id
and model_name. Model settings merge at the top level and are validated at admission;
updates apply to the next run. Only READY permits execution configuration changes;
title remains editable in every phase. Layers and lifecycle cannot be patched.
Lists use creation-time/identity order, offset >= 0 and limit 1..200, returning
`Page(items, has_more)`.

## Native layer scopes and session leases

SessionService and the durable Agent share static typed `layers.registry.LAYERS`.
Factory assembly performs no business I/O. Creation validates Config and no-enter
default States, acquires the session lease, commits CREATING with fixed identity/
Config and empty State, then calls optional enters outside database transactions.
Enter can allocate resources and return required State. Without enter, State is
validated from `{}`. Complete State and READY are published atomically under the
same ownership. Failed initialization records CLOSING and exits every configured
layer in reverse with None, including partial and uncalled enters. No partial
initial State is saved. Compensation continues through hook cancellation; failed
cleanup remains CLOSING for explicit retry. See [layer scopes](../layers/README.md#business-scopes).

Optional fixed creation `id` survives lost responses. Duplicate IDs raise
SessionAlreadyExists without replaying enter. Errors after reservation begins raise
SessionCreationError carrying this ID and the original cause; cancellation propagates.
After an uncertain READY commit, the transaction ends before an owned transaction
probes the authoritative row. READY confirms success; CREATING permits compensation.
An unreachable database leaves the outcome unconfirmed. Query or close that same ID;
there is no enter replay or background sweeper.

`close_session(id)` first acquires the session lease. READY/CLOSING checks active
Temporal execution under the row lock before changing lifecycle; active execution
is busy and query failures propagate. CREATING skips Temporal so abandoned scopes
can be cleaned; CLOSED returns directly. Close records CLOSING, calls layer exits
in reverse, and validates/saves each hook attempt's mutated State under fencing.
Only RetryableScopeError permits three bounded automatic attempts. Exits must be
idempotent and support None for abandoned creation; a later explicit close may
repeat successful exits. Ordinary failures do not prevent the remaining exits.
The session becomes CLOSED after all cleanup succeeds; failures retain CLOSING and
saved progress. Close and interface shutdown do not stop a Temporal Workflow.

Lease heartbeat settings satisfy finite `0 < interval < timeout`; all scope users
sharing a table use compatible policy. Expired-token takeover alone waits the
positive grace period while renewing heartbeat. Ownership checks fence transactions;
heartbeat failure cancels business and normal exit joins heartbeat before conditional
release. Fencing cannot revoke an external request already issued by a lost owner.
Shell jobs persist across successful runs. Failed execution can leave remote jobs
outside the saved inventory, which session close cannot discover.

## History and live output

`read_history(id, before_seq=None, limit=100)` returns the latest page in ascending
seq order. before_seq is exclusive; has_more means older history exists. Missing
session history is empty. The [history repository](../agent_history/README.md)
retains complete messages across close, with permanently sealed positions and gaps.

`live(id, after_seq=-1)` subscribes before replay and follows nonempty event batches.
Consumers apply each batch in order and persist the seq of the last fully applied
complete message. Consecutive broadcasts advance the connection cursor directly;
numeric jumps trigger ordered backfill. Deltas never advance cursors or trigger
reads. Periodic reads recover missed commits even during preview traffic. Complete
messages and stored gaps are immutable. Clear previews on reconnect; discard
previews at/below every applied complete seq and retain later previews. Use aclosing
when stopping early. No transaction spans a yield. History pagination and live
cursors are independent. See [HTTP](../interfaces/http/README.md) and
[output transport](../agent_output/README.md).

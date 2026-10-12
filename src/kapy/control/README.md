# Control services

`ModelService` manages provider/model configuration. `SessionService` manages
session configuration and native layer scopes, admits durable execution through
Temporal, and joins immutable history with live output. The independent Worker
owns the Agent, model and tool resources. A standalone Activity is the only supported runner.

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
from uuid import uuid4


async def run(settings: CommonSettings, provider_id, model_name: str):
    async with open_resources(settings) as resources:
        sessions = create_session_service(resources, settings)
        session = await sessions.create_session(
            CreateSession(provider_id=provider_id, model_name=model_name), activity_id=uuid4()
        )
        return await sessions.start_runner(session.id, activity_id=uuid4(), user_prompt="Hello")
```

## Session state machine

SessionService borrows the database, output service and Temporal client. Each
create/run/close call supplies a UUID `activity_id`; the ID is both the permanent
operation key and native Activity ID. `start_runner` returns `SessionOperation`,
not a native handle. `get_operation(session_id, activity_id)` returns its phase,
optional native run_id and saved result/safe error, without private request/input.
Create and close wait for their immutable SessionRecord result; caller cancellation
does not cancel Worker execution. Failed operations raise OperationFailed.

| From | To | Owner action |
| --- | --- | --- |
| absent | creating | Insert session and pending create operation together |
| creating | ready | Commit all initial States and succeeded result |
| creating | closing | Original running owner compensates failed initialization |
| ready | starting | Claim new run and freeze configuration/messages/State |
| starting | running | Worker claims pending before I/O |
| running | ready | Commit messages, all States and succeeded output |
| creating/starting/running | closing | Confirm failure or seal fixed deadline |
| ready | closing | Claim close before exits |
| closing | closing | New close can claim only after previous operation is terminal |
| closing | closed | Finish all cleanup; successful close or failed-create compensation |
| closed | closed | Read retained state; new close records the already-closed result |

Creating, starting, running and closing with a pending/running operation reject a
different operation with SessionBusy. Same-key intent is checked first, so its
saved result survives later session changes. A key bound to different session,
kind or normalized intent raises OperationConflict. READY edits use conditional
UPDATE; the run's claim returns that same row to freeze input. No transaction spans
remote calls and no explicit SELECT FOR UPDATE or session lease remains.

The Worker validates the actual native Activity identity, claims pending → running
and loads frozen input. All history/final-state/cleanup/result writes conditionally
check the current ID, allowed status, phase and deadline in the same transaction.
Database-internal UPDATE exclusion lasts only through that short transaction.
Terminal phase/result cannot change. Success commits with READY/CLOSED before
Temporal acknowledgment and remains success after a lost acknowledgment.

USE_EXISTING does not validate input and retained-ID rejection expires; therefore
PostgreSQL stores intent, frozen input, a fixed one-hour deadline and terminal
result permanently. RPC uncertainty leaves the owner active and same-key
confirmation uses the original input. Queries/admission reconcile the exact native
run_id and confirmed terminal status; expired active operations seal failed/closing.
NOT_FOUND and RPC failures never mean READY. One Temporal attempt executes the full
run; there is no Agent retry, enter replay, lease takeover or background sweeper.
`is_runner_running` reads database starting/running, without a Temporal RPC.

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
title changes also require READY. Layers and lifecycle cannot be patched.
Lists use creation-time/identity order, offset >= 0 and limit 1..200, returning
`Page(items, has_more)`.

## Native layer scopes

Worker create and close Activities share static definitions with the Agent.
Creation validates configuration and default States before reservation, commits
creating with empty State, then enters scopes outside transactions. All initial
States publish with ready and succeeded together. Failed initialization retains its
running owner during reverse compensation, exits every configured layer with None
including partial/uncalled enters, and records failed after compensation completes.
Complete compensation sets closed; otherwise closing permits a new explicit close.
An uncertain success commit is checked before compensation; unavailable database
leaves the outcome unconfirmed rather than issuing cleanup without ownership.

Close claims ready or an unowned/terminal-owner closing session. Hooks run in reverse
and save validated mutable State after every attempt, including errors/cancellation.
Ordinary failures still try remaining scopes. Only outer RetryableScopeError permits
three local attempts with a half-second delay; its promise is safe hook replay, not
Temporal retry. Storage or validation failure does not retry a hook. Later explicit
close uses a new key and may repeat successful exits, so exits must be idempotent and
support None for abandoned initialization. Fencing stops future calls/writes but
cannot revoke already-issued external requests. See [layers](../layers/README.md).

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

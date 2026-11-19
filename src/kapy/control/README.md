# Control services

`ModelService` manages provider/model configuration. `SessionService` manages
local session configuration and lifecycle, submits Agent runs through Temporal,
and joins immutable history with live output. The independent Worker owns the
Agent and run-local resources.

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
            CreateSession(provider_id=provider_id, model_name=model_name)
        )
        return await sessions.start_runner(session.id, activity_id=uuid4(), user_prompt="Hello")
```

## Session state machine

`create_session` returns a committed `SessionRecord` in `ready`, with a generated
UUID if the caller omitted it. `close_session` commits `closed` and is idempotent.
Closed sessions retain their UUID, configuration and history for read access.
Create raises `SessionAlreadyExists` for an occupied UUID. Edits and submissions
to a closed session raise `LifecycleError`.

`start_runner` takes a caller-supplied Activity UUID and an optional prompt.
It returns `ActivityHandle[str]`. Use a new UUID for each new run and keep the
original UUID on a retry. Temporal keeps the first accepted input per UUID within
its retention window. `get_activity(session_id, activity_id, run_id=...)`
confirms run ownership and returns retained status, run state and available
outcome. Canceling a local wait leaves the Activity running.

The paired `current_activity_id` and `current_activity_run_id` identify the
executing writer while a session is ready.

| Action | Database mutation |
| --- | --- |
| create | Insert ready with validated fixed Config and default States |
| run | Claim the ready row and atomically read model/provider, settings, snapshot and States |
| model → tool/model, tool → model | Save interrupted request snapshot/all States without releasing ownership |
| tool → end | Keep the preceding intermediate snapshot |
| run success | Commit final snapshot and States and clear the writer after resource exit |
| run failure | Clear the writer after resource exit and retain the last saved snapshot, States and history |
| close | Commit a closed tombstone and clear the writer |

Submission does not reserve a session or guarantee order. Several queued Activities
can be accepted; Worker competition chooses the writer, and a loser fails finally
without calling models/tools/hooks. Edits require ready and both identity fields
empty, competing with Worker admission on that same row. Every history/snapshot/
final write checks session ID, full execution identity and ready status
in one short transaction. No transaction spans RPC, hook, model or publication I/O.

Run admission and close can recover an abandoned writer. Both read the old
identity, query that exact execution outside their transaction, and use a
conditional update for a confirmed terminal status or `NOT_FOUND`. A live or
uncertain execution keeps its writer. Changes to either identity field prevent
the conditional update.

Every Activity has one Temporal attempt, a one-hour schedule/start-to-close budget
and heartbeat cancellation. There is no database deadline, local timeout sealing,
background recovery or automatic Agent replay. A business commit can coexist with
a native failure/timeout; current session/history show business facts without
pretending to reconstruct an old Activity result.

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

Configured layers initialize JSON State at creation. Their local handles are
opened and released by the Worker for each run. See [layers](../layers/README.md).

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

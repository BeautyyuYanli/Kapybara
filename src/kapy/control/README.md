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

SessionService borrows database, output and Temporal clients. Each request supplies
an Activity UUID; Temporal owns its retained input, status and outcome. New requests
need new UUIDs; retries keep the original request. Same-key changed prompt/config
uses the first accepted input. Only session/type identity is checked, directly
against Temporal. Deduplication and old-result retrieval end with native retention.

`start_runner` returns `ActivityHandle[str]`. Create and close await native
`handle.result()` and return SessionRecord, or raise a safe SessionExecutionError
with session/Activity/run identities. Canceling a local wait does not cancel remote
execution. `get_activity(session_id, activity_id, run_id=...)` reads native status,
run_state and available outcome; it works before create inserts a business row and
never mutates business data. An unknown record cannot confirm the old outcome.

Sessions use only creating/ready/closing/closed. READY means scopes are initialized,
not that an execution is absent. A paired current_activity_id/current_activity_run_id
identifies the current writer; both clear on normal completion or failed cleanup.
The database stores no queued phase, execution outcome, input, error or deadline.

| Action | Business mutation in the executing Worker |
| --- | --- |
| create | Insert creating, validated fixed Config and native writer; publish all initial States with ready and release |
| run | Claim unowned ready; atomically read current model/provider, settings, snapshot and States |
| run success | Commit snapshot/all States with ready and release, after model/tool clients exit |
| run failure | Preserve preceding snapshot and history; set closing and release after resource exit |
| close | Claim unowned ready/closing, save each exit's progress, then close and release |

Submission does not reserve a session or guarantee order. Several queued Activities
can be accepted; Worker competition chooses the writer, and a loser fails finally
without calling models/tools/hooks. Edits require ready and both identity fields
empty, competing with Worker admission on that same row. Every history/snapshot/
cleanup write checks session ID, full execution identity and permitted lifecycle
in one short transaction. No transaction spans RPC, hook, model or publication I/O.

Only explicit close can recover an abandoned writer. It reads the old identity,
queries that exact native execution outside its transaction, and replaces it by
CAS only for a confirmed terminal status or NOT_FOUND. RUNNING/PAUSED, unknown
nonterminal states and temporary RPC failure preserve the old writer. A changed
identity prevents takeover; an old execution cannot write after replacement.
Queries, configuration edits and run admission never reconcile abandoned ownership.

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

## Native layer scopes

Worker create and close Activities share static definitions with the Agent.
Creation validates configuration and default States before reservation, commits
creating with empty State, then enters scopes outside transactions. All initial
States, READY and ownership release commit atomically as business state; Temporal
records its terminal outcome independently. Failed initialization retains its
writer during reverse compensation and exits every configured layer with None,
including partial/uncalled enters. Temporal records the creation failure.
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

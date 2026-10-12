# HTTP interface

The application lifespan initializes one Temporal Client alongside its database
and Valkey resources. Handlers can borrow it from
`request.app.state.resources.temporal_client`; they must not create a connection
per request. The Temporal service must be reachable at application startup.

`create_router(models, sessions)` returns an APIRouter under `/api`. The separate
`create_model_router` and `create_session_router` omit the prefix for custom mounts.
Constructors borrow services. SessionService borrows the process Temporal Client;
model resources and execution belong to the independent durable Worker.

`kapy interface http serve` runs the standalone application. HTTP and WebSocket
routes are directly accessible without login or an access token.
`KAPY_HTTP_HOST` defaults to 127.0.0.1, `KAPY_HTTP_PORT` to 8000, and
`KAPY_HTTP_SHUTDOWN_TIMEOUT` to 15 seconds. `--host` and `--port` override those
values. Optionally set `KAPY_HTTP_FRONTEND_DIST` or `--frontend-dist` to an existing
Vite build directory to mount the existing frontend at `/app/`.
Run `kapy db upgrade` independently before serving; no schema changes occur at startup.
Shared database/Valkey/runner settings are listed in [interfaces/README.md](../README.md).
The application drains or cancels full ASGI calls, including WebSocket cleanup,
before disposing process-owned connections. It does not load Telegram configuration.

The hosting FastAPI application owns engine/session factory, Valkey client and
Temporal resources through its lifespan. Finish or cancel request tasks
before closing those resources. No authentication, accounts, runner process manager
or old gateway integration is provided.

```python
from fastapi import FastAPI

from kapy.interfaces.http import create_router

# Inside application setup, using resources owned by the application's lifespan:
app = FastAPI(lifespan=lifespan)
app.include_router(create_router(models, sessions))
```

Provider/model CRUD and discover-models endpoints call their identically named
ModelService operations. Models use `/models/{provider_id}/{model_name:path}`;
use the canonical model_name from the returned record, including vendor slashes.
PATCH preserves omitted fields and replaces supplied JSON objects as a whole.
Provider responses return constructor kwargs unchanged and exclude api_key.

Session routes are:

| Method/path below /api | Endpoint | Result |
| --- | --- | --- |
| POST /sessions | create_session | 201 SessionRecord |
| GET /sessions | list_sessions | Page[SessionRecord] |
| GET /sessions/{id} | get_session | SessionRecord |
| PATCH /sessions/{id} | update_session | SessionRecord |
| POST /sessions/{id}/close | close_session | SessionRecord |
| POST /sessions/{id}/runner | start_runner | 202 RunnerRun: activity_id, run_id |
| GET /sessions/{id}/runner | is_runner_running | bool for an active Temporal Activity |
| GET /sessions/{id}/history | read_history | Page[HistoryMessage] |
| WS /sessions/{id}/live?after_seq=N | live_ws | one nonempty OutputEvent[] per text frame |

`POST /sessions` accepts CreateSession and only creates configuration. It does
not submit execution. Model requests are submitted through the runner endpoint.
There are no input queue, steer or cancellation-flag routes.

Creation accepts an optional UUID `id`; retain it across lost responses. Duplicate
IDs return 409 with `session_id` and do not replay scope enter. Errors after reservation begins include that ID in the safe error response,
without upstream payloads. Query GET by this ID before deciding whether to close or
create another session. The host never automatically repeats creation.

`POST /sessions/{id}/runner` accepts `{user_prompt: string}`. It waits for Temporal
start confirmation, then returns Activity ID and actual run ID; 202 does not mean
model completion. An active execution returns 409. Non-successful terminal execution
also returns 409; it cannot restart from the preceding successful snapshot. Each
Agent Activity has one attempt; Worker loss, timeout or cancellation does not resume
its internal steps. A missing Activity record permits a first run only if no
snapshot/version progress or history exists, so expired successful records also
require a new session. Temporal RPC errors return 503 without upstream payloads; a
failed RPC may already have started work.
Query runner status instead of automatically resubmitting an ambiguous request.
Direct submission is not a request-ID deduplication protocol.

Close returns 409 while the durable Activity or a session scope lease is active. It does
not request Temporal cancellation. Retry explicitly when execution finishes.

List providers/models/sessions with offset=0 and limit=100. Page contains only
items and has_more; limit is 1..200. Fetch history without before_seq for the latest
page, then use its earliest seq as exclusive before_seq to load older messages.
Each page's items are ascending, and has_more describes older history. Connect live
with the last fully applied complete message's seq, or -1 without one. after_seq
is required on WebSocket; it is an exclusive cursor, unchanged by upward pagination.
The service subscribes before replay. Consecutive complete broadcasts advance
directly; a numeric jump triggers ordered history backfill. Deltas do not advance
the cursor or trigger reads. Periodic reads recover missed commits during preview
traffic. History may contain numeric gaps; recorded messages and old gaps stay fixed.

WebSocket sends each live batch as a JSON array of existing delta and message DTOs
using the SDK message codec. Apply items in order; frames are neither transactions
nor completion markers. Pending deltas may merge before delivery. The subscriber
continues receiving into its bounded buffer while a frame is being sent.
It does not poll history or signal execution completion. Client business frames
close it with 1003; accepted-connection failures close it with 1011. Disconnect
closes the generator and subscription even when idle, without cancelling the runner.
A connection can span multiple runs. Temporary deltas have no replay guarantee:
clear them on reconnect and resume from the last fully applied complete message.
Only complete messages advance that cursor. After application, discard previews
at/below its seq, including numeric gaps, and keep those at later positions.

Router-local error handling returns FastAPI detail objects: 422 validation,
404 LookupError, 409 identity/lifecycle/execution conflict or busy session, 503 Temporal RPC or
exhausted layer scope retry (retry explicitly), 502 model discovery, otherwise
500. Validation details keep only loc/msg/type; upstream exception text, raw
request data and credentials are not returned. HTTP operation IDs match endpoint
names. The controller owns protocol adaptation; services
own DTOs, parameter constraints, immutable history, replay and execution admission.

Mount the SPA beside the API with `app.include_router(create_frontend_router(dist_dir))`.
The host supplies the explicit Vite build directory and shares its existing resource
lifespan. Missing index.html fails setup. See the root frontend/README.md.

SessionRecord includes lifecycle `status`. Creation first commits creating and
publishes ready only with complete State. Close transitions creating or ready ->
closing -> closed and retains the record. Direct submission and execution
configuration changes require ready; title and read/history/live remain available
in every phase. SDK settings/model availability are checked before execution starts.

CreateSession accepts native `layers` as registered name-to-Config JSON,
for example `{"shell": {"cwd": "/workspace", "base_url": "http://shellctl:8765"}}`.
Unknown names and invalid Config return 422. GET/list/create responses include normalized layer configuration;
internal State is absent. PATCH cannot change layers or State. Durable starts
restore the internal saved State. Optional layer `enter_scope`/`exit_scope` hooks
initialize business State and release registered resources. Enter runs after durable
identity/Config registration under the same lease as ready publication. Lost ready
COMMIT responses are resolved from the authoritative row after the transaction ends;
unconfirmed outcomes never trigger blind resource deletion. Abandoned creating exits
all layers in reverse with None, including partial/uncalled enters, using ID/Config
for recovery. Close retains CLOSING
and validated cleanup progress on failure. Explicit `RetryableScopeError` gets
three bounded hook attempts and returns 503 if exhausted; ordinary hook errors do
not automatically retry. Enter only promises safe replay when explicitly raising
this error; exit must always be idempotent. See [Layers](../../layers/README.md).

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
| POST /sessions/{id}/runner | start_runner | 202 ActivityReference |
| GET /sessions/{id}/activities/{activity_id} | get_session_activity | SessionActivity |
| GET /sessions/{id}/history | read_history | Page[HistoryMessage] |
| WS /sessions/{id}/live?after_seq=N | live_ws | one nonempty OutputEvent[] per text frame |

Create/run/close require an Activity UUID. Its string is the native Activity ID.
Temporal provides deduplication and result retrieval only while records are retained.
Use a new UUID for new work and preserve the original request on retries. Same-key
changed prompt/config uses the first accepted input; another session/type receives
409. Create accepts an explicit session ID, defaulting to its Activity UUID.

Create and close await native results. Safe failure/uncertain responses carry the
session/Activity identity and known run_id for recovery. Run returns 202 with only
`{activity_id, run_id}`: acceptance does not imply business admission or ordering.
Query `/activities/{activity_id}?run_id=...` for Temporal status, available run_state,
execution identity and terminal result/safe failure category. Input and exception
text are never returned. Without run_id the lookup fixes and returns the observed
native run ID. Create is queryable even before its business row exists.

A Worker acquires an unowned ready session only when executing, then loads current
business data/model/provider. Competing runs can therefore fail after 202. READY
with both current identity fields empty permits edits. One Temporal attempt owns
all model/tool resources. A failed run becomes closing; cleanup uses explicit close.
Only close queries and CAS-replaces a terminal/missing old execution. A live/unknown
old execution or RPC failure preserves its occupancy. Reads never reconcile state.

Caller disconnect/cancel does not cancel the Activity. For a lost response, query
the original reference and business resource before deciding on a new request.
NOT_FOUND means no old result can be confirmed; it does not recreate a result from
current business state. A committed business change may coexist with Temporal
failure/timeout, and retained success is not stored permanently by Kapy.

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
404 LookupError, 409 identity/lifecycle/execution conflict or busy session, 503 Temporal RPC, 502 model discovery, otherwise
500. Validation details keep only loc/msg/type; upstream exception text, raw
request data and credentials are not returned. HTTP operation IDs match endpoint
names. The controller owns protocol adaptation; services
own DTOs, parameter constraints, immutable history, replay and execution admission.

Mount the SPA beside the API with `app.include_router(create_frontend_router(dist_dir))`.
The host supplies the explicit Vite build directory and shares its existing resource
lifespan. Missing index.html fails setup. See the root frontend/README.md.

SessionRecord exposes creating/ready/closing/closed and current execution identity. Reads,
history and live remain available throughout; layer Config stays public and fixed,
while State and message snapshots stay internal. Scope Activities run hooks outside
transactions and publish business progress through full writer-identity predicates.
Creation calls `enter_conversation(str(session_id), config)` once per selected hook;
each Agent run passes that same SDK conversation ID. Close and creation compensation
call `exit_conversation` with that ID. Compensation exits every configured layer
with None in reverse order; cleanup progress persists after each attempt.
Only an outer RetryableScopeError
allows the existing three local hook attempts. Failed scope Activities return a
safe native failure; explicit cleanup retry uses a new ID. See
[control state machine](../../control/README.md#session-state-machine) and
[layers](../../layers/README.md).

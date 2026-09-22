# HTTP interface

`create_router(models, sessions, *, agent=None, deps=None, realtime_output=True,
output_flush_interval=0.5)` returns an APIRouter under `/api`. The separate
`create_model_router` and `create_session_router` omit the prefix, for custom mounts.
Constructors borrow services and optional direct Agent/deps. The standalone app
uses the common application execution factory; plugin sessions use that factory.

`kapy interface http serve` runs the standalone application. HTTP and WebSocket
routes are directly accessible without login or an access token.
`KAPY_HTTP_HOST` defaults to 127.0.0.1, `KAPY_HTTP_PORT` to 8000, and
`KAPY_HTTP_SHUTDOWN_TIMEOUT` to 15 seconds. `--host` and `--port` override those
values. Optionally set `KAPY_HTTP_FRONTEND_DIST` or `--frontend-dist` to an existing
Vite build directory to mount the existing frontend at `/app/`.
Run `kapy db upgrade` independently before serving; no schema changes occur at startup.
Shared database/Valkey/runner settings are listed in [interfaces/README.md](../README.md).
The application drains or cancels full ASGI calls, including BackgroundTasks,
before disposing process-owned connections. It does not load Telegram configuration.

The hosting FastAPI application owns engine/session factory, Valkey client and
Agent resources through its lifespan. Finish or cancel request/background tasks
before closing those resources. No authentication, accounts, runner process manager
or old gateway integration is provided.

```python
from fastapi import FastAPI

from kapy.interfaces.http import create_router

# Inside application setup, using resources owned by the application's lifespan:
app = FastAPI(lifespan=lifespan)
app.include_router(create_router(models, sessions, agent=agent))
```

Provider/model CRUD and discover-models endpoints call their identically named
ModelService operations. Models use `/models/{provider_id}/{model_name:path}`;
use the canonical model_name from the returned record, including vendor slashes.
PATCH preserves omitted fields and replaces supplied JSON objects as a whole.
Provider responses return constructor kwargs unchanged and exclude api_key.

Session routes are:

| Method/path below /api | Endpoint | Result |
| --- | --- | --- |
| POST /sessions | create_session_and_schedule | 201 SessionRecord |
| GET /sessions | list_sessions | Page[SessionRecord] |
| GET /sessions/{id} | get_session | SessionRecord |
| PATCH /sessions/{id} | update_session | SessionRecord |
| POST /sessions/{id}/close | close_session | SessionRecord |
| POST /sessions/{id}/inputs | submit_input_and_schedule | 202 SessionInput |
| GET /sessions/{id}/inputs | read_inputs | FIFO SessionInput[]; channel defaults queued |
| DELETE /sessions/{id}/inputs/{input_id} | delete_input | bool; positive input_id |
| GET /sessions/{id}/runner | is_runner_running | bool for an occupied lease, not Agent generation |
| POST /sessions/{id}/cancel | request_cancel | 202 empty body |
| GET /sessions/{id}/cancel | read_cancel | bool |
| GET /sessions/{id}/history | read_history | Page[HistoryMessage] |
| WS /sessions/{id}/live?after_seq=N | live_ws | one nonempty OutputEvent[] per text frame |

CreateSessionAndSchedule extends the existing CreateSession fields with optional
`input: {content, channel="queued"}`. No input means configuration only. With input,
the controller creates the session, submits input, then schedules start_runner if
submit_input observed an idle lease. These are separate committed transactions;
a later failure does not undo the session or input. The inputs endpoint performs
the same submission/scheduling step. HTTP responds without awaiting runner work;
BackgroundTasks are in-process and non-durable, not a job queue. Concurrent startup
intents are resolved by the existing lease. SessionBusy and LifecycleError are absorbed
in the background; other failures are logged by session ID and exception class,
and cancellation propagates.
Close returns 409 while an operation owns a live lease, without starting cleanup
or requesting cancellation. Retry explicitly after the current operation exits.
No independent run endpoint is exposed. Submission is not HTTP-retry-idempotent.

List providers/models/sessions with offset=0 and limit=100. Page contains only
items and has_more; limit is 1..200. Fetch history without before_seq for the latest
page, then use its earliest seq as exclusive before_seq to load older messages.
Each page's items are ascending, and has_more describes older history. Connect live
with the last applied complete message's seq, or -1 for empty history. after_seq is
required on WebSocket; it is an exclusive cursor, unchanged by upward pagination.
The service subscribes before history replay and handles overlaps/backfill.

WebSocket sends each live batch as a JSON array of existing delta and message DTOs
using the SDK message codec. Apply items in order; frames are neither transactions
nor completion markers. Pending deltas may merge before delivery. The subscriber
continues receiving into its bounded buffer while a frame is being sent.
It does not poll history or signal execution completion. Client business frames
close it with 1003; accepted-connection failures close it with 1011. Disconnect
closes the generator and subscription even when idle, without cancelling the runner.
A connection can span multiple runs. Temporary deltas have no replay guarantee:
clear them on reconnect and resume from the last applied complete seq.

Frontends may replace their pending-input list from read_inputs after submissions,
deletions and committed input messages, while merging history by seq. No separate
input/history association or execution outcome state is exposed; a valid lease
neither proves model health nor identifies the preceding run's success.

Router-local error handling returns FastAPI detail objects: 422 validation,
404 LookupError, 409 identity/lifecycle conflict or busy close, 503 plugin close failure (retry
the same close call), 502 model discovery, otherwise
500. Validation details keep only loc/msg/type; upstream exception text, raw
request data and credentials are not returned. HTTP operation IDs match endpoint
names. The controller owns protocol adaptation and background scheduling; services
own DTOs, parameter constraints, queue consumption, replay and execution handoff.

Mount the SPA beside the API with `app.include_router(create_frontend_router(dist_dir))`.
The host supplies the explicit Vite build directory and shares its existing resource
lifespan. Missing index.html fails setup. See the root frontend/README.md.

CreateSession accepts fixed `plugins` (provider/name/config); SessionRecord returns
`status`. Close transitions ready -> closing -> closed and retains records. Intake
and background runner scheduling require ready; read/history/live and ordinary
configuration changes remain available after close. Model references may dangle;
SDK settings/model availability are checked before execution consumes input.

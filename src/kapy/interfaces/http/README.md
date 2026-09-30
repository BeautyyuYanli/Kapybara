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
| POST /sessions | create_session_and_schedule | 201 SessionRecord |
| GET /sessions | list_sessions | Page[SessionRecord] |
| GET /sessions/{id} | get_session | SessionRecord |
| PATCH /sessions/{id} | update_session | SessionRecord |
| POST /sessions/{id}/close | close_session | SessionRecord |
| POST /sessions/{id}/inputs | submit_input_and_schedule | 202 SessionInput |
| GET /sessions/{id}/inputs | read_inputs | FIFO SessionInput[]; channel defaults queued |
| DELETE /sessions/{id}/inputs/{input_id} | delete_input | bool; positive input_id |
| POST /sessions/{id}/runner | start_durable_runner | 202 DurableRun: workflow_id, run_id |
| GET /sessions/{id}/runner | is_runner_running | bool for an active Temporal Workflow |
| POST /sessions/{id}/cancel | request_cancel | 202 empty body |
| GET /sessions/{id}/cancel | read_cancel | bool |
| GET /sessions/{id}/history | read_history | Page[HistoryMessage] |
| WS /sessions/{id}/live?after_seq=N | live_ws | one nonempty OutputEvent[] per text frame |

CreateSessionAndSchedule retains its existing optional
`input: {content, channel="queued"}`. Creation and optional input submission use
separate committed transactions. The inputs endpoint retains queued/steer storage,
query and withdrawal semantics, including multimodal UserInput. Enqueueing never
starts a runner. Existing operation IDs retain their names for client compatibility.
Cancel endpoints store/read the legacy flag; durable execution does not consume it.

`POST /sessions/{id}/runner` accepts `{user_prompt: string}`. It waits for Temporal
start confirmation, then returns Workflow ID and actual run ID; 202 does not mean
model completion. An active execution returns 409. Non-successful terminal execution
also returns 409; it cannot restart from the preceding successful snapshot. Worker
recovery and Activity retries remain in the original Workflow. Temporal RPC errors
return 503 without upstream payloads; a failed RPC may already have started work.
Query runner status instead of automatically resubmitting an ambiguous request.
Direct submission is not a request-ID deduplication protocol.

Close returns 409 while the durable Workflow or a legacy lease is active. It does
not request Temporal cancellation. Retry explicitly when execution finishes.

List providers/models/sessions with offset=0 and limit=100. Page contains only
items and has_more; limit is 1..200. Fetch history without before_seq for the latest
page, then use its earliest seq as exclusive before_seq to load older messages.
Each page's items are ascending, and has_more describes older history. Connect live
with the last fully applied authoritative prefix's seq, or -1 without one. after_seq is
required on WebSocket; it is an exclusive cursor, unchanged by upward pagination.
The service subscribes before replay. Consecutive authoritative broadcasts advance
directly; a larger jump triggers ordered history backfill. Preview events do not
advance or trigger reads. Periodic reads recover missed commits even during preview
traffic. Sequence gaps proven by history are valid; provisional messages may repeat
or be overwritten. An authoritative message after a provisional history row is a
contract error and ends the stream instead of skipping that row.

WebSocket sends each live batch as a JSON array of existing delta and message DTOs
using the SDK message codec. Apply items in order; frames are neither transactions
nor completion markers. Pending deltas may merge before delivery. The subscriber
continues receiving into its bounded buffer while a frame is being sent.
It does not poll history or signal execution completion. Client business frames
close it with 1003; accepted-connection failures close it with 1011. Disconnect
closes the generator and subscription even when idle, without cancelling the runner.
A connection can span multiple runs. Temporary deltas have no replay guarantee:
clear them on reconnect and resume from the last fully applied authoritative prefix. Delta and provisional
message positions never advance that cursor. After fully applying an authoritative
message, discard all provisional snapshots and previews at/below the new cursor,
including numeric gaps; storage may delete them without a deletion event. Keep
previews at later positions. Legacy append-only consumers may
continue using their applied seq but must deduplicate repeated provisional history.

Frontends may replace their pending-input list from read_inputs after submissions,
deletions and committed input messages, while merging history by seq. No separate
input/history association or execution outcome state is exposed; an active Workflow
does not prove model health.

Router-local error handling returns FastAPI detail objects: 422 validation,
404 LookupError, 409 identity/lifecycle/execution conflict or busy session, 503 Temporal RPC or
plugin close failure (retry the same close call), 502 model discovery, otherwise
500. Validation details keep only loc/msg/type; upstream exception text, raw
request data and credentials are not returned. HTTP operation IDs match endpoint
names. The controller owns protocol adaptation; services
own DTOs, parameter constraints, queue consumption, replay and execution handoff.

Mount the SPA beside the API with `app.include_router(create_frontend_router(dist_dir))`.
The host supplies the explicit Vite build directory and shares its existing resource
lifespan. Missing index.html fails setup. See the root frontend/README.md.

CreateSession accepts fixed `plugins` (provider/name/config); SessionRecord returns
`status`. Close transitions ready -> closing -> closed and retains records. Intake
and direct runner submission require ready; read/history/live and ordinary
configuration changes remain available after close. Model references may dangle;
SDK settings/model availability are checked before direct execution starts. Business
plugin bindings and legacy checkpoints cannot start durable execution. Context and
compaction settings retain their CRUD behavior but are unused by the durable Worker.

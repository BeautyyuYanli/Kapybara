# State module

`kapy.state` owns PostgreSQL sessions, the injected runner lifecycle, inputs, output/history,
one-shot waiting channels. Public dataclasses and runner methods are in
`src/kapy/state/contracts.py`; `SessionService` and `migrate` are exported from the package.
The public interaction model is defined by `docs/contracts.md`, including typed output and
one-shot reply addresses.
Gateway owns authentication, caller/target separation, session authorization and channel endpoints.
State has no owner, parent, scope, grants or authentication token DTO.

## Assembly and lifetime

Call `await migrate(database_url, schema="kapy_state")`, then enter
`async with SessionService(database_url=..., valkey_url=..., runner=..., schema=...,
namespace=...) as service`. Each module runs its own migrations. The service owns a
psycopg async pool (maximum 12), a dedicated advisory-lock connection, one actual
`valkey.asyncio.Valkey` client/PubSub and its background/runner tasks. Instances are entered once;
construct another instance after exit. The database and login role must already exist.
Migrations create only the specified schema, apply ordered SQL in one transaction and verify
checksums. Production roles need privileges only for their module schema and advisory locks.

One controller owns a schema. Startup takes a session advisory lock and changes a durable epoch;
all writes acquire the service-meta row lock and verify that epoch. Runner writes also verify
run id and attempt. The lease is checked at least once per scan with a two-second deadline;
lease failure closes admission and cancels local runners. No model/machine I/O or Valkey call
holds a State database write transaction. Different session runners can overlap; one session's
runner never overlaps its successor. Short state commits serialize across the schema. A reply can settle multiple inputs in one transaction; no throughput guarantee is implied.

Valkey publishes a small `changed` hint on `<namespace>:wake`. PostgreSQL is authoritative.
Hints are coalesced and do not contain work or cursor positions. Local notifications and a
maximum one-second scan wake committed work even when every hint is lost. The PubSub loop
reconnects independently. The service neither creates Valkey work keys nor flushes the server.
Closing cancels and awaits tasks, then closes PubSub, the client, the pool and lease connection;
it retains waiting channels and uncompleted runs for recovery. Output observers wake with
ServiceUnavailable when the service closes.

## Transactions, input and recovery

create/input/publish/update/delete take a UUID `request_id`. Matching retries return the first durable receipt;
different operation/parameters under the same UUID raise Conflict. Initial runner state is
excluded from create's retry fingerprint: the first successful initialization wins. Update is
a full replacement of mutable settings while waiting; omitted output_mode preserves its
creation-time value. Update/delete check their durable receipt
before the current session state, so an old retry returns its original result even after newer
settings or deletion. Deletion commits an intent before cancellation and atomically stores its
original boolean result with final cleanup; interrupted intents resume at startup.

`wait_submission(session_id, request_id, wait_seconds=0)` observes the persisted create/input
receipt as `SubmissionStatus(submission, completion)`. Completion contains run id, outcome, output,
cursor and completion timestamp. It never registers a subscription or consumes an event/input.
Multiple observers and restart retries see the same completion, including after session deletion.
A timeout returns `completion=None`; unknown or wrong-target requests raise NotFound. Shutdown
wakes pending observers with ServiceUnavailable.

Starting a run reserves up to 64 pending inputs ordered by the session sequence. `poll_steer`
reserves up to 64 new steer inputs while queue inputs remain pending until waiting. Reservations
are acknowledged only by the same transaction that commits the runner checkpoint and complete
model messages. Checkpoint numbers strictly increase; retrying any committed number with the
same content returns its original cursor. Emission ids deduplicate output. The runner must finish
with a next-number final checkpoint that acknowledges every outstanding reservation.

Session config fixes `output_mode` at creation (`text` by default, or `reply_to`). Updates
cannot change it. Each direct input creates a fresh one-shot reply channel and returns its
`Submission.waiting_id`. It is the receiver's `SessionInput.being_waited_id`; client-selected
waiting IDs are not accepted. Empty creation returns `submission=None` and creates no channel.
Retries with the same request ID and arguments return the same address.

`RunResult(output, checkpoint)` carries a `SessionOutput`: `str | WaitFor | ReplyTo`.
`WaitFor(waiting_ids)` requires 1–128 distinct UUIDs and replaces the active waiting set.
It never settles input replies. Text mode ends with text and settles every consumed unresolved
input, including earlier runs. Reply mode ends with `ReplyTo(being_waited_ids, payload)` and
settles only the selected consumed unresolved inputs. The maximum is 128 distinct addresses;
an empty reply selection is valid only when no read inputs await reply. Unselected inputs
remain unresolved until future direct input or waiting results continue the session; they do
not cause automatic runs. Waiting inputs have no new reply address.

Pydantic AI output functions expose only an `ids` array to the model. The reply function fills
its DTO from the latest complete visible model text in this run and returns the complete DTO
as framework output. State routes by its type and addresses and persists the entire output;
it does not unwrap `ReplyTo.payload`. Text and reply endings clear active waits. A waiting
record marks a run boundary and by itself never publishes a result.

Successful finish atomically commits the checkpoint, complete output, run boundary, selected
request completions and ready-channel handoffs. Consumption means an input is in a durable
checkpoint; it does not mean the input was replied to. Runner's paged
`unreplied_addresses(after=0, limit=64)` reads only this session's consumed unresolved addresses,
ordered by input sequence. Reply mode puts these addresses in the model instructions. Text
mode does not expose addresses or the reply tool/prompt. Compression preserves complete typed
outputs and protects cycles containing unresolved inputs.

Each channel has a unique producer, at most one receiver, and an immutable receiver binding.
Its lifecycle is `open → ready → delivered`. Publication before waiting stores a ready result;
waiting hands it into the receiver's durable input queue. Channel ID is also the input's
`event_id`, protected by a unique constraint. The handoff and terminal channel state commit in
one transaction. Retry receipts are repeatable, but another logical publication, receiver or
handoff is rejected. Cancelling an active wait never releases receiver ownership. No session
has a default channel, automatic self-listening or default completion publication. A producer
that is explicitly also its receiver follows the same rules as other endpoints.

`publish_event(waiting_id, payload, ...)` publishes once on an independent external channel;
input-linked reply channels can only be settled by State. Gateway checks the producer and
receiver principals. A waiting input contains:

```json
{
  "type": "waiting",
  "waiting_id": "<channel UUID>",
  "producer_session_id": "<producer UUID or null>",
  "output": {"kind": "reply_to", "being_waited_ids": ["<input address>"], "payload": "reply text"},
  "outcome": "completed"
}
```

`output` is the entire original result (a string, complete typed output, or external JSON).
`outcome` is `completed`, `failed` or `deleted`. Failures/deletion have a null output; they are
control outcomes rather than a third model output tool. Replies enter the receiver as steer;
external publication may explicitly choose queue. Already handed-off inputs survive wait
replacement and are never retracted. Remaining active waits survive a wake until the next
successful output replaces or clears them.

Completion output has one authoritative copy in `waiting_channels.output`; request rows
retain only completion metadata. `wait_submission` joins the channel and never consumes it.
Migration 002 installs the new schema only when there are no old sessions, requests or channel
rows. Existing old business state is unsupported and causes the migration transaction to fail;
it is neither converted nor cleared. Old Gateway channel metadata is likewise rejected.
Runner accepts only `kapy.agent.v2` snapshots. Use a new schema for the new protocol; no runtime
adapter interprets old prompts, final outputs, wait calls or receipt payloads.

A normal runner exception, including a runner-raised NotFound or ServiceUnavailable, produces
a sanitized error and failed completion for all consumed unresolved inputs, including prior waits, then permits queued work to run.
Failure completion first verifies that the service still runs and the durable attempt is active;
actual deletion, lease loss and cancellation cannot produce a spurious failed completion. Shutdown/cancellation/abrupt process exit leave the run
recoverable: the next owner keeps its run id, increments attempt, appends interrupted output and
supplies the last checkpoint plus unacknowledged reservations. Previously consumed inputs remain
in Intelligence's saved state. State does not repeat external commands or claim exactly-once
external effects. Intelligence checkpoints tool intent/result and reconciles unknown outcomes
with Execution. Deletion first fences the session, then cancels its runner outside the transaction,
then publishes deleted completions and removes only that session's rows. Startup finishes a
persisted deleting state. It does not kill machine processes or delete skills/work directories.

## Output, search and limits

Cursors encode version/session/sequence. They are opaque positions, not authorization tokens.
Every record sequence is allocated transactionally per session. Read pages bind an upper snapshot
before fetching records, so concurrent writes cannot move a returned cursor past unread output.
`read_output(after=None)` replays from the beginning; continue from `next_cursor`, then use the
same API's bounded long poll for live output. Full message records and their deltas share a
message id for frontend replacement. Raw history is append-only, unaffected by model compression.
Database read rows are assembled into dataclasses without ORM or Pydantic validation.

`read_history`, `search_history`, `export_history`, and the logical relation available to
`query_history` include only `input`, `model_request`, `model_response`, `final`, `waiting`,
and `error` records. They omit delta/notice/tool-stream records and `interrupted` markers.
Use `read_output` for complete output replay, including deltas and interrupted attempts.
Filtered history pages share the session sequence space and may advance their cursor across
omitted records; a history/search/export cursor is therefore not evidence that all output
through that position has been read. Keep the `read_output` cursor for replay-to-live consumption.

`export_history` returns `HistoryExportPage(items, next_cursor, snapshot_cursor, has_more)`.
Its first page captures the current history upper cursor; supply that same `snapshot` and the
previous `next_cursor` for each next page. Later appends are excluded. Cursor/session mismatches,
positions beyond the snapshot, and snapshots beyond the current session are rejected. Deleting
a session removes its records, so subsequent export calls return NotFound; this API does not
freeze a session or retain deleted history.

Output, history/search and SQL pages have a **512 KiB** encoded JSON cap including DTO/pagination
fields. Page budgeting includes the longest snapshot/next cursor and the larger terminal
`has_more=false` representation, including export metadata. Size calculation includes actual
UTF-8 bytes and JSON escaping, using conservative
ASCII escaping and normal JSON separators. Server cursors stop before collecting an oversized
page; a single oversized SQL row is rejected after fetching it. Record pages continue at complete
record boundaries. Limits: 200 rows/page; 256 KiB per input, event or complete message; 16 KiB per
delta (including metadata); 4 MiB per checkpoint; 128 waiting ids; 30 seconds per long poll. A record
must also fit a complete page, so a payload near its individual cap can be rejected if duplicated
searchable text/envelope overhead would exceed that page. This check also runs before accepting
backlog, so a large event cannot poison future delivery. PostgreSQL-incompatible NUL/surrogate
strings and nonfinite JSON numbers are rejected before writing. Media is persisted as references.
History and waiting channels have no implicit TTL.

Substring uses parameterized literal `strpos`, so `%` and `_` are ordinary characters. Keyword
search normalizes NFKC/casefold, indexes Unicode words with PostgreSQL's built-in simple GIN
text search, and supplements CJK words with characters/bigrams plus literal normalized substring
verification. It supports examples in Chinese, Japanese, Korean, Latin, Arabic and Cyrillic;
it does not promise stemming, semantic search or complete linguistic segmentation.

History SQL is a closed SELECT language over
`history(seq,run_id,kind,message_id,text,data,created_at)`. A recursive SQLGlot AST whitelist
checks every node and option, emits new SQL with safe identifiers and bound values, and rewrites
every history reference (including joins/subqueries) to one service-owned MATERIALIZED CTE
filtered by the authorized session. It does not execute the caller's SQL text. INNER/LEFT joins,
scalar/FROM subqueries, EXISTS/IN, comparisons, grouping/order/limit and count/min/max/lower/
length/coalesce are supported. Unknown functions, schema/physical/system tables, CTEs/UNION,
DDL/DML, casts, windows, LATERAL, table functions, locking and unknown syntax are rejected.
The transaction is read-only with `pg_catalog` search path, 2-second statement timeout and
250 ms lock timeout. Input SQL is at most 16 KiB, 256 AST nodes, four SELECT nesting levels and
four relation references. SQL row-count truncation is explicit; byte excess raises QueryLimitExceeded.
Database errors are mapped to State errors without exposing original SQL or connection strings.

## Verification

Run `uv run ruff check src/kapy/state tests/state`,
`uv run pyrefly check src/kapy/state tests/state`, and
`uv run pytest -q -s tests/state`. `tests/state/pyrefly.toml` supplies only the test package's import
roots; no shared configuration or dependencies changed. Tests read `KAPY_DATABASE_URL` and
`KAPY_VALKEY_URL` from the process environment, without loading `.env`. When unset, they fall back
to `postgresql://kapy:kapy-local@127.0.0.1:55432/kapy` and `redis://127.0.0.1:56379/0`.
The recovery subprocess receives those same effective addresses explicitly in its environment.
Each test uses a new `state_test_<uuid>` schema/namespace; cleanup drops only that exact owned
schema. Recovery kills only a dedicated control subprocess; no shared database or Valkey
restart/flush is performed. No Telegram messages are sent.

The load tests print accepted/completed/replayed counts, ordering and timings for 100 sessions
with 20 inputs each, and distinct deliveries/consumption/latency for 100 event listeners.
Measurements are evidence for that local fake-runner workload, not production performance claims.


Runner code can raise `RunFailure(code, public_message)` for a known, nonsecret failure.
The code is an ASCII identifier of at most 64 characters; public_message is 1–1024 UTF-8
bytes without NUL. State records `{kind: code, public_message}` and uses the explanation
as failed Completion.output. This is an explicit trust boundary: callers must not wrap
arbitrary SDK/exception strings. Other exceptions retain a generic error and empty failed
completion output. State does not interpret provider or frontend configuration.

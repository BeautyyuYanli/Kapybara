# State module

`kapy.state` owns PostgreSQL sessions, the injected runner lifecycle, inputs, output/history,
subscriptions and sticky events. Public dataclasses and runner methods are in
`src/kapy/state/contracts.py`; `SessionService` and `migrate` are exported from the package.
The implementation uses the approved `1fbe9a5` interfaces with a 512 KiB page amendment.
Gateway owns authentication, caller/target separation, session authorization and channel grants.
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
runner never overlaps its successor. Short state commits serialize across the schema. A broadcast
or backlog drain transaction grows with the affected audience and backlog, so large backlogs
can delay unrelated writes; no throughput guarantee is implied.

Valkey publishes a small `changed` hint on `<namespace>:wake`. PostgreSQL is authoritative.
Hints are coalesced and do not contain work or cursor positions. Local notifications and a
maximum one-second scan wake committed work even when every hint is lost. The PubSub loop
reconnects independently. The service neither creates Valkey work keys nor flushes the server.
Closing cancels and awaits tasks, then closes PubSub, the client, the pool and lease connection;
it retains subscriptions and uncompleted runs for recovery. Output observers wake with
ServiceUnavailable when the service closes.

## Transactions, input and recovery

create/input/publish take a UUID `request_id`. Matching retries return the first durable receipt;
different operation/parameters under the same UUID raise Conflict. Initial runner state is
excluded from create's retry fingerprint: the first successful initialization wins. Update is
a full replacement of mutable settings while waiting, and delete is naturally idempotent by
session id. The frozen initial proposal does not expose `wait_submission`, `export_history`,
or request-id-bearing update/delete overloads. Gateway can observe waiting output without
joining the event subscription system; a completion is also persisted internally on its request.

Starting a run reserves up to 64 pending inputs ordered by the session sequence. `poll_steer`
reserves up to 64 new steer inputs while queue inputs remain pending until waiting. Reservations
are acknowledged only by the same transaction that commits the runner checkpoint and complete
model messages. Checkpoint numbers strictly increase; retrying any committed number with the
same content returns its original cursor. Emission ids deduplicate output. The runner must finish
with a next-number final checkpoint that acknowledges every outstanding reservation.

Successful finish atomically commits final state/output, enters waiting, replaces external
subscriptions, drains eligible backlog, and publishes completion for requests consumed by the
run plus the default channel. An early child completion waits durably until the parent subscribes.
A late steer or queue input is not completed by an earlier run. Natural completion uses an empty
external wait set. Subscriptions persist while the runner works and through process restarts;
a later successful wait result replaces them. Removing a subscription does not retract inputs
already delivered. Default subscriptions remain until deletion.

Publishing delivers once to every currently subscribed session except the trusted producer id.
No eligible subscriber (including only the producer) leaves an event pending. The first later
eligible subscription hands the backlog to the then-current audience; subscribers arriving after
that durable handoff do not retroactively receive it. Handoff creates durable inputs in the same
transaction and marks the event delivered. This is the event acknowledgement; frontends read
output and never consume agent subscriber queues.

A normal runner exception produces a sanitized error and failed completion for its accepted
inputs, then permits queued work to run. Shutdown/cancellation/abrupt process exit leave the run
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

Output, history/search and SQL pages have a **512 KiB** encoded JSON cap including DTO/pagination
fields. Size calculation includes actual UTF-8 bytes and JSON escaping, using conservative
ASCII escaping and normal JSON separators. Server cursors stop before collecting an oversized
page; a single oversized SQL row is rejected after fetching it. Record pages continue at complete
record boundaries. Limits: 200 rows/page; 256 KiB per input, event or complete message; 16 KiB per
delta (including metadata); 4 MiB per checkpoint; 128 waiting ids; 30 seconds per long poll. A record
must also fit a complete page, so a payload near its individual cap can be rejected if duplicated
searchable text/envelope overhead would exceed that page. This check also runs before accepting
backlog, so a large event cannot poison future delivery. PostgreSQL-incompatible NUL/surrogate
strings and nonfinite JSON numbers are rejected before writing. Media is persisted as references.
History and pending events have no implicit TTL.

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
roots; no shared configuration or dependencies changed. Tests use the architect's loopback
PostgreSQL/Valkey with a new `state_test_<uuid>` schema/namespace per test. Cleanup drops only
that exact owned schema. Recovery kills only a dedicated control subprocess; no shared database
or Valkey restart/flush is performed. No Telegram messages are sent.

The load tests print accepted/completed/replayed counts, ordering and timings for 100 sessions
with 20 inputs each, and distinct deliveries/consumption/latency for 100 event listeners.
Measurements are evidence for that local fake-runner workload, not production performance claims.

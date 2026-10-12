# Core and interface migrations

Core PostgreSQL and each interface's private storage have independent Alembic
environments and version tables. Model definitions stay in their owning modules.
The shared adapter receives a connection, metadata, script directory, version table
and ownership predicate; it knows no interfaces or dialect configuration.

```sh
kapy db upgrade
kapy db current
kapy db revision --message 'Describe the core change'
kapy interface telegram db upgrade
kapy interface telegram db current
kapy interface telegram db revision --message 'Describe the Telegram change'
```

Upgrade initializes an empty schema through the initial revision or advances a
versioned schema to head. Serve never migrates. Core upgrade creates the configured
PostgreSQL schema when absent, without changing interface databases. Telegram only
opens its own SQLite file. Unversioned/pre-baseline databases are not silently
stamped or converted; recreate those development databases. Stopping processes
never drops data. The process manager's SQLite is outside these histories.

Current core metadata consists of ControlTable and history_metadata.
Current tables are providers, models, sessions, agent_history and session_operations.
`OWNED_TABLES` also retains removed/renamed table names so autogeneration can discover
removal. Reflection excludes unrelated tables and schemas. Core versions live in
`database/migrations/` with `core_schema_version`; Telegram versions live in
`interfaces/telegram/migrations/` with `plugin_telegram_schema_version`, scoped to
`plugin_telegram_*`. HTTP has no private database history. SQLite uses batch
rendering for structural changes.

## Durable snapshots, layers and history

Sessions retain opaque runner_state TEXT, initially NULL, fixed Config and mutable
layer State JSON. current_activity_id identifies the operation allowed to write.
The permanent session_operations ledger stores normalized intent, frozen input,
phase, native run_id, fixed deadline and immutable result/safe error. Private input
includes provider credentials and is never returned by public DTOs. Temporal input
is only the UUID. Run final save commits snapshot, all States, ready and succeeded
output together; cleanup saves individual State progress in owned short transactions.
No snapshot version or session lease protocol remains.

The session-operations revision moves existing nonclosed sessions to closing,
preserving snapshots, history and State for reads/explicit cleanup. It does not
fabricate completion evidence or call remote hooks. It adds the operation ledger
and current ID, removes session_leases and runner_state_version, and rejects
downgrade because earlier ownership cannot be reconstructed. Generate it against
a disposable preceding schema with `kapy db revision --message '...' --plan
session-operations`; revisions are generated, not edited manually.

This ownership migration requires old interfaces/Workers stopped and old Activities
drained or ended. The SQL changes ownership records; it cannot stop execution.
New code must use the new schema and UUID-only Activity inputs together: preceding
RunnerInput payloads and lease/version-based Workers are incompatible with it.

Shell layer State requires `scope_id` alongside `jobs`; its enter hook initializes
the UUID from the session identity. The data-only revision fills a missing field
in an existing shell State from that row's session UUID, preserving jobs, other
layer States, configuration, runner snapshots and their versions. Creating or
abandoned closing rows without shell State stay empty. Downgrade removes only
scope_id from shell State.

Generate against a disposable schema at the preceding revision:

```sh
kapy db revision --message 'Record shell scope identity in layer state' --plan shell-scope-state
```

The explicit plan requires no structural schema differences and emits fixed SQL.
Run this migration with runners and interfaces stopped. It converts current
database State, without rewriting recorded Temporal inputs or results;
historical payloads missing scope_id have no runtime compatibility inference.

`agent_history` retains complete SDK messages keyed by `(session_id, seq)`.
Under the session row lock, append inserts only above the batch's pre-write maximum.
Stored positions and gaps remain sealed. Columns decide position, while SDK
metadata.seq marks successful recording in the opaque snapshot. Read/codec behavior
is described in [agent_history](../agent_history/README.md).

## Removing legacy execution data

Revision `5de14eebe20b` removes agent_states, agent_context_pages,
plugin_agent_bindings, session_inputs and session_cancels. It explicitly drops both
compaction CHECK constraints and the sessions.context_plugin,
compaction_threshold_tokens and compaction_replay_turns columns.

Before dropping the identifying tables, fixed SQL marks every session with a
checkpoint or plugin binding CLOSED. It preserves session identity, history and
any existing durable snapshot/version rather than fabricating a snapshot from
incompatible recovery data. Other session lifecycle, layer Config/State, history
messages/timestamps and lease rows remain intact. Closed legacy sessions can still
be queried and replayed; execution requires a new session. Removed queue and cancel
data has no durable consumer and is discarded. The revision is irreversible.

Run this upgrade offline with interfaces and runners stopped and in-flight
Workflows drained. Clean legacy plugin resources using the preceding release
before dropping their State; the migration cannot execute removed business hooks.
No online compatibility layer or old Workflow replay adapter is provided.

Generate the revision against a disposable schema at `da2b698fbbac`:

```sh
kapy db revision --message 'Remove legacy runner and context configuration' --plan durable-only
```

`revision_plans.durable_only` accepts only the complete expected legacy removal,
reflects the two named compaction checks and prepends session closure. Generated
operations are fixed and never import this plan or current models. Historical
revisions remain unchanged. A generated merge revision joins `4a86521aa2ff`
(shell identity) and `5de14eebe20b` (durable-only removal), retaining one head.
Upgrading a database on either branch also applies the other revision.

## Generating revisions

Use a development checkout with locked dependencies and a disposable database at
the preceding revision. Never generate against a deployment schema. Alembic's
post-write hooks fix imports and format the generated file with Ruff. Never edit
revisions by hand; change ORM/generation inputs and regenerate an unpublished
revision. Every historical script fixes its own schema operations. Data conversion
or changes Alembic cannot infer need an explicit generation plan.

The retained historical plans document earlier offline conversions:

```sh
kapy db revision --message 'Generalize summaries to context pages' --plan context-pages
kapy db revision --message 'Make complete history immutable' --plan immutable-history
kapy interface telegram db revision --message 'Use complete message delivery cursors' --plan immutable-history
```

The page revision renamed agent_compactions and converted summary text to
summary/v1 JSON at the same session/anchor/timestamp. Its downgrade permits only
that summary protocol. The immutable-history revision removed authority flags,
retained confirmed history and checkpoint-session history, removed unconfirmed
durable requests and unmarked their snapshot entries while keeping SDK content.
It did not change snapshot versions. Telegram's matching conversion folded sent
reply receipts into after_seq and kept unfinished pending-send offsets. These
conversions also require stopped interfaces/runners and reject irreversible
downgrades. The merged head removes legacy page/checkpoint storage and fills missing shell
scope identity while retaining the complete history produced by those revisions.

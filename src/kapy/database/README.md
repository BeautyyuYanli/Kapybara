# Core and interface migrations

Core PostgreSQL tables and each interface's private storage have independent Alembic
environments and version tables. Model definitions stay in their owning modules.
The generic migration adapter only receives a connection, metadata, script directory,
version-table name and table-ownership predicate; it knows no interfaces or dialect setup.

```sh
kapy db upgrade
kapy db current
kapy db revision --message 'Describe the core change'
kapy interface telegram db upgrade
kapy interface telegram db current
kapy interface telegram db revision --message 'Describe the Telegram change'
```

Upgrade creates a fresh database's tables through the initial revision, or advances
an already versioned database to head. Serve never performs a migration. For the
initial development baseline, discard older databases and initialize empty storage;
no conversion or preservation of pre-baseline data is supported. Existing tables
are deliberately not auto-stamped or overwritten. Core upgrade may create
the configured PostgreSQL schema, but never creates or migrates an interface database.
Telegram commands only open its SQLite file. Stopping a process never drops data.

Core metadata is explicitly ControlTable, agent_metadata and lease_metadata.
Sessions also store `context_plugin` JSON with a server default of kapy/summary and
empty config, backfilling existing session rows through the generated column revision.
This selection does not add plugin-owned tables or change context-page payloads.
Sessions also retain the latest opaque `runner_state` TEXT and its BIGINT
`runner_state_version`. The generated column revision initializes existing rows
to NULL/0, without deriving state from legacy history. The runner owns encoding;
the session repository owns versioned, idempotent replacement. These columns are
internal storage and are absent from ordinary session configuration DTOs.

Sessions store native layer configuration in `layers` JSON and internal execution
State in `layer_states` JSON. The generated column revision gives both independent
`{}` defaults and backfills existing rows with empty selections. Configuration
names are fixed on creation. Successful tool calls return State to the Workflow
without accessing these columns. The final successful snapshot saves all States
and the opaque messages together using the existing version. Single-layer database
updates are reserved for CLOSING session cleanup progress. Retry identity includes
both snapshot contents. Generate this schema change with
`kapy db revision --message 'Add session layer configuration and state'` against a
disposable schema at the preceding revision. Layer State introduces no separate
database, StateStore, or plugin-owned tables.

`agent_history` stores fixed complete messages keyed by `(session_id, seq)`.
Under the session row lock, durable writes insert only above the batch's pre-write
maximum seq; old rows and their numeric gaps remain sealed. SDK `metadata.seq`
marks successful recording for restoration and numbering, while storage uses
columns rather than JSON payload to decide positions.

The offline immutable-history revision removes the old authority column/index.
It preserves every response and confirmed request, and all history belonging to
legacy checkpoint sessions. Unconfirmed durable requests are removed; matching
runner_state messages retain their content but lose seq. Retained positions,
content, timestamps and runner-state versions stay unchanged, and obsolete
metadata authority flags are removed. Telegram's separate revision folds sent
reply receipts into after_seq, clears covered pending sends, retains unfinished
body/format/character offsets, and drops receipt/edit state. Run these migrations
with runners and interfaces stopped. They have no online compatibility or old
Workflow replay adapter. Both data conversions reject downgrade because discarded
provisional records and receipt state cannot be reconstructed.

Generate each revision against a development database at its preceding revision:

```sh
kapy db revision --message 'Make complete history immutable' --plan immutable-history
kapy interface telegram db revision --message 'Use complete message delivery cursors' --plan immutable-history
```

The explicit plans reject unrelated schema differences and emit fixed SQL before
the generated column/index removal; generated revisions import no current models
or conversion plans.

ControlTable includes `plugin_agent_bindings` and the shared session/binding lifecycle
columns. Agent plugins store small JSON state in this core-owned table; data_version
migrations transform individual binding data on context loading and are separate
from Alembic schema upgrades. Telegram private SQLite remains independent.
Sessions add creating before any layer enter. They persist fixed identity/Config with
empty layer_states, then atomically publish complete State with ready. No partial
initialization State or extra progress table is stored. The shared non-native enum
now needs VARCHAR(8); its generated revision expands both lifecycle columns while
retaining existing data/status. Legacy bindings still use ready/closing/closed.
`session_leases` independently stores ownership; `agent_states` stores checkpoint
position without lock fields. The stable OWNED_TABLES
catalog retains removed table names so future autogenerated drops stay visible.
Core version history is `database/migrations/`, with `core_schema_version`.
Telegram history is `interfaces/telegram/migrations/`, with
`plugin_telegram_schema_version`; comparison is restricted to `plugin_telegram_*`.
Each history starts with the initial development baseline; later changes append
revisions. Superseded pre-baseline development revisions are not retained. Databases stamped with a discarded
development revision must be recreated from the initial revision.
Reflection excludes other schemas and unrelated tables. SQLite uses Alembic batch
rendering for structural changes. HTTP has no private tables or empty migration chain.

Generate revisions in a development checkout (`uv sync --group dev`) through
these commands and review the generated operations. Alembic post-write hooks run
Ruff import fixes and formatting as part of generation.
Never edit generated revisions by hand; adjust the ORM or generation inputs and
regenerate an unpublished revision when necessary. Each history script fixes its
operations and does not import current ORM models. Production data migrations or
changes Alembic cannot infer require an explicit generation design before use.
The local process manager's SQLite database is outside these version histories.

The core page revision renames agent_compactions to agent_context_pages, retaining
session, anchor and created_at while converting text to summary/v1 JSON payload.
The removed table name remains in OWNED_TABLES for autogeneration ownership. Page
state belongs to execution recovery, not a transport interface.

This conversion is generated on a development database at the preceding revision:

```sh
kapy db revision --message 'Generalize summaries to context pages' --plan context-pages
```

The explicit `revision_plans.context_pages` input accepts only the expected old-table
drop/new-table create comparison and replaces it with rename/conversion operations.
Any unrelated schema difference rejects generation. Generated revisions contain
fixed operations, never runtime imports of that plan. Downgrade preserves summary/v1
rows and rejects other page protocols rather than discarding their payloads.

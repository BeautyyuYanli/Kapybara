# Kapy Agent Plugins

`AgentPluginService` hosts builtin resource tools in the Agent process. It is
independent of HTTP/Telegram process entrypoints under `kapy.interfaces`. An
application registry maps `(plugin_provider, plugin_name)` to one current trusted
`PluginDefinition`: Pydantic config/state types, a no-argument implementation,
positive `data_version`, and optional pure JSON migrations. Register installed
implementations in `application.agent.create_registry`, or inject a registry into
`application.sessions.create_session_service`. No sample/resource plugin is
registered automatically.

A `CreateSession.plugins` item contains only provider, name and config. Duplicate
identities, missing definitions and invalid config fail before persistence. The
session and all bindings are inserted ready in one transaction, with null state.
Bindings/config choices are fixed; there is no attach/detach or runtime version
selection. `list_bindings` reads records without triggering migration.

Each fresh execution or close constructs a plugin with no arguments. Constructors
only initialize memory. Both methods receive `SessionContext(session_id,
plugin_provider, plugin_name, config, state)`; no execution UUID, SDK RunContext,
database, service container, logger or resource backend is injected.

- `open_execution(ctx)` is an async context manager yielding `PluginBinding` with
  an optional asynchronous instructions function and typed `PluginTool` functions.
  It owns local connections/tasks until exit. Session resources may be allocated
  lazily here or in tools, registered promptly in state, and reused on later runs.
- `close_session(ctx)` waits for all registered session-owned resources to be
  deleted or confirmed absent, and finishes its local cleanup before returning.
  It must tolerate empty/partial state, repeated calls and concurrent deletion.
  The base implementation is a no-op for plugins without session cleanup.

The execution factory enters all plugins in identity order inside the runner
lease, builds a fresh Agent and exits in reverse after SDK graph cleanup. Factory
failure prevents input consumption. State is never implicitly saved. Instructions
may only read state/generate text: repeated model or compaction evaluation must
not allocate resources. The adapter rejects instruction state writes. Typed tools
use native SDK validation and `PrefixTools`; final names are
`provider_plugin_tool`, ASCII alphanumeric/underscore, at most 64 characters.
Collisions (including ambiguous underscore concatenations) fail; no renaming occurs.
Removed tools/invalid old arguments follow SDK bounded retry semantics during
checkpoint recovery. Data migrations never rewrite model messages.

`StateStore.read` returns an independent typed value and UUID revision.
`replace(value, expected_revision=...)` validates outside the transaction, then
atomically replaces state and revision. A stale UUID raises `StateConflict`:
plugins reread and decide how to merge. UUIDs have no ordering/count semantics.
The store is fixed to one binding, operation and data format; it becomes invalid
when its context ends. Each operation owns a separate short database transaction;
plugin callbacks never receive a transaction or create nested savepoints.

Before forming each context, the service loads raw config/state and chains pure
synchronous migrations `n -> n+1`, then validates current types. Only when an
upgrade is needed does it atomically persist both JSON values, the target data
version and a new UUID using the original version/revision as CAS. Same-version
loads only validate, preserving the stored JSON and revision.
Missing steps, invalid data or newer persisted formats
fail without partial changes. Conflicts reread/recompute. Custom validators and
migration functions must perform no I/O or external mutations; they run outside
transactions, without a plugin instance or SessionContext.

Session and binding statuses share `LifecycleStatus`: ready -> closing -> closed.
Ready permits execution; it does not promise resources already exist. Close locks
the session row briefly to record an irreversible decision and move all unclosed
bindings to closing, also requesting runner cancellation. It then awaits one
plugin at a time, persists each closed binding, and stops at the first failure.
Retry skips closed bindings. Only once all bindings are closed is the session
closed; history, input and ownership records remain queryable. Close does not
wait for the runner, take its distributed lease, or run a task group.

State writes, input intake/consumption and closing lock the same session row.
Execution state access is permitted only while ready. After closing wins,
execution `replace` raises `LifecycleError` without changing state/revision;
retrying CAS cannot bypass that decision. Close-scope stores may still save cleanup
progress while the session and their binding are closing. Concurrent closers may
observe another completed binding and continue, but unrelated cleanup/database
errors still propagate. Cancel/timeout stops the current close and preserves its
committed progress. Plugins must join their tasks and use bounded local cleanup.

External allocation and registration are not atomic. Plugins coordinate parallel
first use, promptly record resource IDs, and on rejected/cancelled registration
best-effort delete newly unregistered resources using locally held references.
An ambiguous commit must not cause blind deletion of a possibly registered
resource. Tag external resources with session/provider/name ownership and provide
reconciliation or a reliable TTL for late/orphaned creates. Closed guarantees only
registered cleanup, not absence of every orphan or completion of in-flight work.
The generic host has no resource inventory interpreter, reaper or reconciliation
scheduler. Process shutdown releases execution clients/tasks, not session resources.

Builtin code is trusted Python: this restricted API is not a security sandbox.
Future untrusted implementations must run in isolation with trusted host-generated
Capability/tool proxies and JSON-only bidirectional StateStore RPC. Identity,
operation and permissions bind to the channel, never caller-supplied strings.
Plugin custom validators and migrations must execute there too, without importing
untrusted types/callables into the host. Migration has no StateStore/context,
credentials, external network or writable resource access; the host only validates
trusted JSON/schema constraints and commits version/revision CAS. Isolation must
cover filesystem, credentials, process permissions and network, including direct
DB/HTTP access. RPC, sandbox, installation, quotas and large files are deferred.

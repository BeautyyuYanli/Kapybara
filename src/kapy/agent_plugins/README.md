# Kapy Agent Plugins

`AgentPluginService` hosts builtin resource tools in the Agent process. It is
independent of HTTP/Telegram process entrypoints under `kapy.interfaces`. An
application registry maps `(plugin_provider, plugin_name)` to one current trusted
`PluginDefinition`: Pydantic config/state types, a no-argument implementation,
positive `data_version`, and optional pure JSON migrations. Register installed
implementations in `application.agent.create_registry`, or inject a registry into
`application.sessions.create_session_service`. The default registry includes
`builtin.shell`; sessions opt in explicitly, so existing sessions gain no tools.

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
  an optional asynchronous instructions function, typed `PluginTool` functions,
  and a tuple of trusted native SDK capabilities.
  It owns local connections/tasks until exit. Session resources may be allocated
  lazily here or in tools, registered promptly in state, and reused on later runs.
- `close_session(ctx)` waits for all registered session-owned resources to be
  deleted or confirmed absent, and finishes its local cleanup before returning.
  It must tolerate empty/partial state, repeated calls and concurrent deletion.
  The base implementation is a no-op for plugins without session cleanup.

The application execution factory opens the configured model and all plugins in
identity order inside the runner lease. It creates a base Agent with model,
settings and static instructions, collects each plugin's capabilities into
RunnerExecution, and exits in reverse after SDK graph cleanup. The runner installs capabilities when opening the graph; plugins and
the base Agent do not install business contributions. Factory failure prevents
input consumption. State is never implicitly saved. Declarative instructions
may only read state/generate text: repeated model or compaction evaluation must
not allocate resources. The adapter rejects instruction state writes. Typed tools
use native SDK validation and `PrefixTools`; final names are
`provider_plugin_tool`, ASCII alphanumeric/underscore, at most 64 characters.
The adapter returns the prefixed declarative capability followed by the binding's
native capabilities in declared order. Native capabilities retain their original
wrapper relationships and receive no automatic prefixes, scope checks, or
instruction read-only guard. They are trusted SDK code, must supply valid final
tool names themselves, and may only use captured resources within this execution.
The model-request boundary checks names after all SDK toolset wrappers, and the
SDK rejects collisions between native and declarative contributions. The host
does not rename tools.
Removed tools/invalid old arguments follow SDK bounded retry semantics during
checkpoint recovery. Data migrations never rewrite model messages.

SessionExecutionCapability wraps all business capabilities, so response rewrites
and after-node work complete before the session checkpoint. Plugins need no
dependency on that host class. Collection order defines the composition chain;
before hooks run forwards, after hooks backwards, and wrap hooks nest. Use SDK
get_ordering() for real dependencies; conflicting constraints fail without a
separate priority system. Instances belong to an execution and may span queued
and auxiliary paging runs; use for_run() to isolate each run's mutable state.

`StateStore.read` returns an independent typed value and UUID revision.
`replace(value, expected_revision=...)` validates outside the transaction, then
atomically replaces state and revision. A stale UUID raises `StateConflict`:
plugins reread and decide how to merge. UUIDs have no ordering/count semantics.
The store is fixed to one binding and data format; it becomes invalid
when its context ends. Each operation owns a separate short database transaction;
plugin callbacks never receive a transaction or create nested savepoints. The
host borrows the admitted operation's SessionLease, locking its row before every
state read/write and migration/completion commit. Same-owner transactions serialize,
but revision CAS still rejects stale snapshots from parallel tools.

Before forming each context, the service loads raw config/state and chains pure
synchronous migrations `n -> n+1`, then validates current types. Only when an
upgrade is needed does it atomically persist both JSON values, the target data
version and a new UUID using the original version/revision as CAS. Same-version
loads only validate, preserving the stored JSON and revision.
Missing steps, invalid data or newer persisted formats
fail without partial changes. Migration runs once before the binding opens;
ownership loss aborts its fenced save without retry. Custom validators and
migration functions must perform no I/O or external mutations; they run outside
transactions, without a plugin instance or SessionContext.

Session status advances ready -> closing -> closed. Ready permits execution; it
never promises resources already exist. Close first acquires the same lease as
execution. A live owner raises SessionBusy without changing session, bindings,
cancel or inputs. After admission, close locks the lease and session rows to record
its irreversible closing decision, then sequentially awaits each unfinished
binding. Each successful callback is fenced and recorded closed. The binding set
is fixed, so completing that list suffices to close the session. Retry skips closed
bindings; legacy closing bindings are simply unfinished. New closes do not write
binding closing states. History, input and ownership records remain queryable.

Execution admission checks ready once under its lease. That lifecycle stays stable
through model/tool/paging and execution cleanup; normal close cannot overlap it.
The application explicitly passes its lease to the plugin host, while plugin-facing
SessionContext and callbacks expose no lease. Input intake still locks/checks the
session in its own transaction. Expired-owner takeover renews during a default
30-second grace before admission; it does not prove the old process exited. Both
runner and plugin host fence every protected transaction on the same lease row,
so persistent ownership safety does not depend on that timing assumption.
Heartbeat failure cancels business through the lease's AnyIO task group. Plugins
must join their tasks and use bounded local cleanup. Errors/cancellation preserve
committed progress; close never requests or consumes runner cancellation.

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

## builtin.shell

The default registry exposes a shellctl-backed plugin. Enable it when creating a
session, for example:

```json
{
  "plugins": [{
    "plugin_provider": "builtin",
    "plugin_name": "shell",
    "config": {
      "base_url": "http://127.0.0.1:8765",
      "cwd": "/work",
      "env": {},
      "token_env": "SHELLCTL_AUTH_TOKEN",
      "redact_patterns": []
    }
  }]
}
```

`cwd` is required and means an existing absolute POSIX directory on the shellctl
machine. Other fields have the defaults above. Config validation is pure and
rejects unknown fields, invalid URLs/regexes, invalid environment entries, and
the reserved `KAPY_SESSION_ID`, `KAPY_PLUGIN_PROVIDER`, `KAPY_PLUGIN_NAME` names.
The plugin adds those metadata fields to every job for server-side identification.
The token is read from the plugin process at each execution/close; missing means
unauthenticated access. Its value is not persisted or sent to job environments.
`env` is ordinary persisted JSON, not a secret store.

The borrowed service is the independently started
[shellctl server](../../../packages/shellctl/README.md) with its matching SDK.
Neither registration nor execution starts the service or creates/deletes its cwd. No Dify Agent Stub,
automatic CLI installation, machine provisioning or extra file tools are implied.

| Agent tool | Behavior |
| --- | --- |
| `builtin_shell_run(script, timeout=30)` | Create a PTY job in configured cwd/env |
| `builtin_shell_wait(job_id, timeout=30)` | Read after the saved byte cursor |
| `builtin_shell_input(job_id, text, timeout=30)` | Send PTY input once and read output |
| `builtin_shell_interrupt(job_id, grace_seconds=5)` | Terminate, retaining job/log references |

Timeout is a server output-wait budget, not job lifetime. Bounds/defaults reuse
the Python SDK: at most 300 seconds, wait/grace allow zero, run/input require a
positive value; 8 KiB response pages and a 0.5-second idle flush. Tool validation
uses the native capability adapter. The model supplies neither cursors nor
server/config overrides. Unknown IDs are rejected before HTTP; these checks are
an interface ownership convention, not a sandbox for arbitrary shell scripts.

Observations contain `<metadata>` and `<output>`, with UTF-8-safe 4 KiB head/tail
excerpts for long output and the remote full-log path. Truncated pages optionally
fetch the latest tail and advance the cursor past omitted middle bytes. Tail
failure preserves the original page/cursor. Configured regexes and the known
token literal are redacted before clipping; this does not discover unknown secrets
or secrets split across server pages. Operational HTTP errors become model-visible
errors; lifecycle, state writes and cancellation propagate. Side effects are not
retried automatically, and a failed run may already have created a job.

State is only `jobs: {job_id: byte_offset}` in the existing binding JSON. CAS merges
preserve other jobs, advance cursors by maximum, and never resurrect removed IDs.
Parallel interactions on the same job can repeat output; order dependent calls
must await previous results. A definite `job_not_found` removes stale inventory.
Execution exit closes its HTTP client and retains registered jobs for later
executions. Finished jobs/logs may expire under the service's GC policy sooner.

Close sequentially force-deletes known jobs with zero terminate grace, saving each
success/not-found removal. Failure/cancellation stops and preserves remaining
references for retry. It has a 60-second work budget plus at most 5 seconds of
shielded client cleanup; it does not delete business files, cwd or the server.
Known rejected allocations get at most 10 seconds of shielded best-effort cleanup.
Ambiguous registration commits are read back before deciding to delete; if state
cannot be checked, a potentially registered job is left intact and the error
retains its ID. An unknown ID after a lost run response requires reconciliation
using shellctl job `.job-env.json` ownership metadata. Terminal GC does not expire
running orphans. There is no global job-list sweep or background reaper.

Cleanup completion follows shellctl's delete acknowledgement: upstream attempts
tmux cleanup, deletes the record, and best-effort removes artifacts. It does not
prove every detached OS descendant exited or every file was removed.

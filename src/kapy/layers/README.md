# Kapy layers

Kapy consumes the independent [Pydantic AI Layer Protocol](../../../packages/pydantic-ai-layer-protocol/README.md).
The package owns Layer definitions, registry assembly, Config/State validation,
native capability composition and optional Temporal State transport. The shell
implementation, installed factory mapping, scope retry policy and SessionService
remain in Kapy.

`registry.layer_registry` is a process-local `TemporalLayerRegistry` constructed
with `{"shell": shell.make}` and Kapy's RunnerDeps.layers adapter once.
The Agent installs it directly; SessionService and Workflow use its
`definitions`, `prepare_configs`, `load` and `snapshot` methods.
SessionService, Workflow and Activity use these same static definitions. Only the
adapter knows the deps layout; shell obtains all data through injected access and
Config/State. Changing the installed set requires reassembly.

## Business scopes

A factory explicitly supplies either or both async functions on its Layer:

```python
async def enter_scope(scope_id: UUID, config: Config) -> State: ...
async def exit_scope(scope_id: UUID, config: Config, state: State | None) -> None: ...
```

The layer's scope ID is the host session UUID, stable across runs and process
restarts. Session creation validates all Config and no-enter default States, then
acquires the session lease and commits `creating` with its identity and fixed Config.
Only acknowledged registration permits enters, in configured order outside database
transactions. A valid returned State
is the initial State; its fields need not have defaults. Without enter, the host
initializes State from `{}`. Hook parameters are validated model copies; persisted
Config and State remain JSON objects. SDK component construction and client close
are separate lifetimes and do not enter or exit the business scope.

Complete State is saved atomically with `ready` under that same lease; partial
initialization State is never saved. Failed/cancelled initialization first persists
`closing`, then calls every configured exit in reverse order with None, including
failed and uncalled enters. Exit must clean by scope ID and Config without State;
None does not mean no resources. It must safely handle absent resources and repeated
calls. One failed exit does not prevent other exits. Cleanup failure leaves durable
`closing` for explicit close, while the original creation error remains the cause.
After a ready COMMIT error, the original transaction ends before a new protected
transaction resolves the authoritative row: ready is retained, creating is closed.
Unavailable storage leaves the outcome unconfirmed and does not trigger blind exit.

Session close obtains the existing exclusive lease, checks active Workflows for
initialized sessions, and persists `closing` before calling exits outside database
transactions. An exit may
mutate State to record confirmed cleanup. The host validates and saves that State
under the lease after every attempt, including error or cancellation. Storage or
validation failures stop retrying that layer; remaining layers are still attempted.
All cleanup must succeed before `closed`;
failure keeps `closing` and any saved progress. A repeated close can call an earlier
successful exit again; an already closed session returns without calling hooks.

Only an explicit `pydantic_ai_layer_protocol.RetryableScopeError` automatically retries a hook,
at most three attempts separated by 0.5 seconds. Use exception chaining to retain
the underlying cause. Ordinary errors, invalid State and cancellation are not
retried; nested causes and exception groups do not imply retryability. Every retry
uses the same scope ID and Config. Normal close exits retain revalidated, persisted
cleanup State; creation-failure compensation uniformly uses None. Enter need not
be globally idempotent: throwing RetryableScopeError explicitly promises safe
repetition of that call. Exit is always idempotent, including partial effects,
lost acknowledgements and repeated close. A restarted host closes abandoned creating
records rather than replaying enter. If remote creates can complete late, the service
must terminally close scopes or the layer must reclaim late resources. The lease
fences local writes and cannot revoke an already-issued external request.
The host supplies no generic resource manager or background scope workflow.

CreateSession accepts an optional fixed UUID. Duplicate creation reports that
identity without replaying enter; callers query or close the same
ID. HTTP creation errors after reservation begins include the ID, hide upstream
details and report exhausted retryable errors as 503. Telegram saves the ID before
creation and queries it on inbox recovery. Neither interface automatically repeats
creation after the hook's bounded attempts.


## Shell layer

`shell.layer.make` returns `Layer[D, Config, State]` with the shared shell models,
fixed native Toolset and explicit enter/exit hooks. It imposes no deps structure;
injected access defines where its data lives. Create a durable session with:

```json
{"layers": {"shell": {"cwd": "/workspace", "base_url": "http://shellctl:8765"}}}
```

`cwd` must be an absolute POSIX directory that already exists on the remote
server. Config also accepts `env`, optional `token_env`, and `redact_patterns`.
The default server is `http://127.0.0.1:8765`. The process executing a tool call
or scope exit reads the named token environment variable only at that boundary.
State is `{"scope_id": "session-uuid", "jobs": {"job-id": byte_offset}}`; it contains
no clients or secrets. Enter records its scope UUID without remote I/O. Every tool
reads that identity from State for remote `KAPY_SESSION_ID` ownership metadata;
it never reads a host identity field from deps. State requires scope_id, including
when restoring saved JSON; there is no runtime inference for older payloads.

The tools retain the existing names and parameters: `builtin_shell_run`,
`builtin_shell_wait`, `builtin_shell_input`, and `builtin_shell_interrupt`.
Run records a returned job ID before reading output. Wait/input resume from its
saved byte cursor; cursors never decrease. Interrupt retains the job and its
readable logs. Confirmed `job_not_found` removes the entry. Output limits,
truncation, tail supplementation and redaction follow `layers.shell.output`.

Each tool owns and closes its HTTP client, with bounded shielded cleanup. The
remote job survives client close and successful runner completion. `enter_scope`
starts an empty inventory with its scope identity. `exit_scope` deletes known jobs one
by one, forgetting confirmed deletions and `job_not_found` results; the host saves
that progress after each attempt. Transport/timeout and temporary availability
failures explicitly opt into bounded retries; authentication and other errors do
not. Failed cleanup leaves CLOSING and remaining jobs for an explicit close retry.
Each exit attempt has a 60-second budget; client close remains bounded. It owns
neither the shellctl server nor its directories and files. Ambiguous remote acknowledgements
can leave an unrecorded job or an already applied input; there is no remote
exactly-once protocol. For abandoned initialization, shell exit accepts None and
makes no request because enter allocated no resources and tools could not run before
ready. Ready close requires its valid saved jobs inventory. shellctl's jobs listing
has no scope ownership field; cleanup cannot discover jobs missing from the snapshot.

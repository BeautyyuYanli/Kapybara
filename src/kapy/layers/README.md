# Kapy layers

Kapy consumes the independent [Pydantic AI Layer Protocol](../../../packages/pydantic-ai-layer-protocol/README.md).
The package owns Layer definitions, registry assembly, Config/State validation,
native capability composition. The shell
implementation, installed factory mapping, scope retry policy and SessionService
remain in Kapy.

`registry.layer_registry` is a process-local `LayerRegistry` constructed
with `{"shell": shell.make}` and Kapy's RunnerDeps.layers adapter once.
The Agent installs it directly; SessionService and Activity use its
`definitions`, `prepare_configs`, `load` and `snapshot` methods.
SessionService and Activity use these same static definitions. Only the
adapter knows the deps layout; shell obtains all data through injected access and
Config/State. Changing the installed set requires reassembly.

## Business scopes

A factory explicitly supplies either or both async functions on its Layer:

```python
async def enter_scope(scope_id: UUID, config: Config) -> State: ...
async def exit_scope(scope_id: UUID, config: Config, state: State | None) -> None: ...
```

The scope ID is the host session UUID, stable across runs and process restarts.
SessionService validates Config and no-enter default States, then atomically
reserves creating with fixed Config and a pending create operation. Its UUID
activity_id is the Temporal ID and permanent database key. A Worker Activity claims
pending before enter hooks; hooks never hold a database transaction. Without enter,
State is initialized from `{}`; returned State can require fields without defaults.
Config and State remain JSON, while SDK component/client lifetimes stay separate.

Complete State, ready and the saved creation result commit together. Failed creation
moves to closing while retaining its running operation through compensation, exits
all configured layers in reverse with None, including partial/uncalled enters, then
records failed/closed or failed/closing. None does not mean no remote resources.
An uncertain ready commit is checked before compensation; a committed success
cannot be overwritten or cleaned up by that failed acknowledgment.

Close claims ready or closing whose previous operation is terminal, using a new ID.
Exit hooks run in reverse and save validated mutable State after each attempt,
including errors/cancellation. Ordinary failures still attempt remaining exits;
all cleanup must succeed for closed. Failure saves progress and closing. Same-key
terminal close returns its prior result; a new explicit close may repeat successful
exits, so exits must always be idempotent and accept None for abandoned initialization.

Only outer `RetryableScopeError` allows three hook attempts separated by 0.5 seconds.
Nested causes/groups, invalid State and ordinary errors are not retry signals.
The fixed ID/Config survives those attempts; State progress is validated and saved
before the next exit attempt. Storage/validation failure does not retry a hook.
This is the existing business-hook contract; Temporal itself uses one attempt and
never restores the Agent or automatically replays enter.

Every new scope/model/tool call checks current_activity_id, operation phase and
fixed deadline. Progress/result writes conditionally check them in one short
transaction, without SELECT FOR UPDATE or a lease. A stale owner cannot issue
further calls or write back; already-issued remote I/O cannot be revoked. Layers
must reclaim late allocations or provide terminal scope closure by ID/Config.
Caller disconnect does not stop Worker hooks; actual Activity cancellation executes
owned compensation/failure handling. There is no generic resource manager,
background scope recovery or Workflow.

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

Tools explicitly run sequentially because their State shares one job inventory.
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

# Layers

A layer is an installed Python module selected by a plain `name -> module`
registry. It exports Pydantic `Config` and `State` classes and exactly one native
Pydantic AI `Capability` or `Toolset` class. The component constructor takes its
registered name, retains that name, and uses it as its stable SDK ID. There is no
configuration factory or instance stored in the registry.

Tools (their collection, names and argument schemas) and instructions are static
declarations, stable for fixed Config. Do not generate them through State-driven
factories: changing the model prefix defeats prefix caching. SDK callbacks can
render those fixed declarations; dynamic business information belongs in State
and tool results.

`deps.layers` is a JSON dictionary. Each name has a `config` object and a `state`
object. Validators, constructors and instructions must be deterministic and
perform no external I/O, because Temporal reconstructs components in both
Workflows and Activities. Config stays fixed during execution. State is a
validated copy and requires explicit JSON writeback:

```python
class Toolset(FunctionToolset[LayerDeps]):
    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(id=name)
        self.add_function(self.count, name="example_count", sequential=True)

    async def count(self, ctx: RunContext[LayerDeps]) -> int:
        entry = ctx.deps.layers[self.name]
        config = Config.model_validate(entry["config"])
        state = State.model_validate(entry["state"])
        state.calls += config.increment
        entry["state"] = state.model_dump(mode="json")
        return state.calls
```

`load_layers(configs, states, registry)` validates exports and names, fills
default State from `{}`, and returns independent JSON copies. Unconfigured state
names fail. `create_layers(deps, registry)` creates fresh native components by
name. `snapshot_layers(deps, registry)` validates and copies only the current
States. Both models must serialize to JSON objects.

## Business scopes

Modules may independently export either of these async functions:

```python
async def enter_scope(scope_id: UUID, config: Config) -> State: ...
async def exit_scope(scope_id: UUID, config: Config, state: State | None) -> None: ...
```

The layer's scope ID is the host session UUID, stable across runs and process
restarts. Session creation validates all exports/Config and default States, then
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

Only an explicit `kapy.layers.RetryableScopeError` automatically retries a hook,
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
identity without replaying enter or initial input; callers query or close the same
ID. HTTP creation errors after reservation begins include the ID, hide upstream
details and report exhausted retryable errors as 503. Telegram saves the ID before
creation and queries it on inbox recovery. Neither interface automatically repeats
creation after the hook's bounded attempts.

`layers_capability(registry)` captures the module mapping at Agent construction
and installs one stable `DynamicCapability(id="layers")`. Empty deps enables no
layers. A Toolset is wrapped in a native capability; native capability hooks,
instructions, tool flags and resource lifecycles remain SDK-owned. Layer
instructions have no application ordering contract. SDK hook ordering, including
the durable message recorder, still applies.

Layer composition uses one patched combined capability in ordinary runs and
Temporal Activities. Every capability that contributes a Toolset, including
nested owners, must declare a nonempty stable ID. Ownership uses that declared ID,
so it does not need the live registry omitted by `TemporalRunContext`. The patch
inherits the SDK's ownership wrapper and retains its deferred instruction
collection: both capability and Toolset instructions remain available when loaded.
The patch copies the SDK's `get_tools` definition processing because owner-ID
resolution has no override hook. [SDK contract tests](../../../tests/layers/test_sdk_contract.py)
compare it with the installed native implementation and require review when the
copied method or ownership fields change. A guard failure requires reviewing the
upstream change before updating its fingerprint.

These examples use deterministic SDK models and show fixed exports, explicit
State writes, enabling/disabling selections, and resource cleanup:

```sh
python -m kapy.layers.examples.in_process
python -m kapy.layers.examples.temporal
```

Standalone Temporal examples demonstrate composition only. SDK tool Activities
receive serialized deps and return tool results; modified Activity deps do not
return to the Workflow. Consumers that need State must own its transport boundary.
Kapy's [durable runner](../runner_duarable/README.md) carries successful State with
the native tool return, applies it before Workflow hooks, and persists it with
the final run snapshot. Failed/cancelled tool calls return no State.

## Shell layer

`registry.REGISTRY` registers `"shell": shell.layer`. The module exports
`Config`, `State`, `Toolset`, `enter_scope`, and `exit_scope`. Create a durable session with:

```json
{"layers": {"shell": {"cwd": "/workspace", "base_url": "http://shellctl:8765"}}}
```

`cwd` must be an absolute POSIX directory that already exists on the remote
server. Config also accepts `env`, optional `token_env`, and `redact_patterns`.
The default server is `http://127.0.0.1:8765`. The process executing a tool call
or scope exit reads the named token environment variable only at that boundary.
State is `{"jobs": {"job-id": byte_offset}}`; it contains no clients or secrets.

The tools retain the existing names and parameters: `builtin_shell_run`,
`builtin_shell_wait`, `builtin_shell_input`, and `builtin_shell_interrupt`.
Run records a returned job ID before reading output. Wait/input resume from its
saved byte cursor; cursors never decrease. Interrupt retains the job and its
readable logs. Confirmed `job_not_found` removes the entry. Output limits,
truncation, tail supplementation and redaction are shared with the legacy shell
plugin in `agent_plugins.builtin.shell`.

Each tool owns and closes its HTTP client, with bounded shielded cleanup. The
remote job survives client close and successful runner completion. `enter_scope`
starts an empty inventory without remote I/O. `exit_scope` deletes known jobs one
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

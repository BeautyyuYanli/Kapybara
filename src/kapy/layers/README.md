# Layers

A `Layer[D, Config, State]` is a static definition containing Pydantic model
classes, one native `AbstractCapability[D]` instance and two independently
optional business-scope hooks. An installed factory has the signature
`make(layer_id: str, access: LayerAccess[D]) -> Layer[D, Config, State]`.
The concrete return annotation links the hook parameters and asynchronous return
values to the models; exit must accept `State | None`.

`make_layers(registry, get_layers)` assembles every installed factory once,
without execution deps. It checks the returned Layer, model classes, native
capability, async hooks and root capability ID. The root ID must equal the
registered nonempty string; the framework never rewrites it. Function tools inherit
the Capability ID during SDK construction. Nested leaves and internal Toolsets
may use independent or derived stable IDs; every registered capability leaf must
have one, including leaves without tools. IDs and composition membership stay
fixed after construction. Native Combined containers do not need extra IDs.

The caller supplies `get_layers(deps) -> dict[str, LayerData]`. Its mapping is the
current enabled selection, separate from the installed factory allowlist. Each
injected `access(deps)` returns the actual writable entry for its own ID, without
copying it or creating missing entries. Kapy's `registry.REGISTRY` installs
`"shell": shell.make` and `"mcps": mcps.make`; its process-local `LAYERS` uses the `RunnerDeps.layers`
adapter. SessionService, Workflow and Activity use that same static configuration;
independent consumers assemble their own definitions and may store data elsewhere.
Changing the installation set requires reassembly.

Each entry is a JSON `config` object and `state` object. Config stays fixed for
the session. Models read from current data are disposable copies; mutation requires
an explicit writeback, which updates deps but does not itself save to a database:

```python
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability
from kapy.layers import Layer, LayerAccess, LayerData, make_layers
from kapy.layers.modules import dump_object

class Deps(BaseModel):
    layer_data: dict[str, LayerData]

class Config(BaseModel):
    increment: int = 1

class State(BaseModel):
    calls: int = 0

def make_counter(layer_id: str, access: LayerAccess[Deps]) -> Layer[Deps, Config, State]:
    def count(ctx: RunContext[Deps]) -> int:
        entry = access(ctx.deps)
        config = Config.model_validate(entry["config"])
        state = State.model_validate(entry["state"])
        state.calls += config.increment
        access(ctx.deps)["state"] = dump_object(state)
        return state.calls

    return Layer(config_type=Config, state_type=State,
                 capability=Capability(id=layer_id, tools=[count]))

def get_layers(deps: Deps) -> dict[str, LayerData]:
    return deps.layer_data

layers = make_layers({"counter": make_counter}, get_layers)
```

Factories, access rules, validators and instruction callbacks are deterministic
and perform no external I/O. Static definitions never retain a session's deps,
models, scope ID or clients. Tools (collection, names and argument schemas) and
instructions stay stable for fixed Config. SDK callbacks may render those fixed
declarations; dynamic business information belongs in State and tool results,
so it does not change the model prefix.

`prepare_layer_configs(configs, layers)` validates all names and Config before
scope hooks, without constructing State. `load_layers(configs, states, layers)`
validates and copies existing JSON; omitted State validates from `{}` and fails
when required fields are missing. It calls no scope hooks. Unconfigured State and
unknown names fail. `snapshot_layers(deps, layers, get_layers)` validates and copies
only the selected States. Both models must serialize to JSON objects; inputs are
copied before validation so custom validators cannot mutate caller-owned data.

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

`layers_capability(layers, get_layers=get_layers)` prepares a static selector.
At Agent construction it exposes all installed entries so the SDK can register
Activities. For each run it validates current Config/State and selects enabled
definitions in the deps mapping's order; an empty selection enables zero layers.
Unknown names or invalid data fail before capability execution. Factories are not
rerun. Each registered leaf is wrapped in a native `DynamicCapability` whose
Activity ID equals that leaf's stable capability ID. Missing leaf IDs fail during
selector preparation. A Wrapper over one leaf is wrapped as a whole; Combined
containers and Wrappers containing children retain their native structure.

Native Combined owns composition, instructions, hook ordering and tool ownership
in ordinary runs and on the Temporal Workflow side. Native Dynamic rebuilds each
leaf inside Activities, which do not receive the live capability registry.
Successful discovery returns tool definitions for native Workflow-side ownership
and deferral processing. Deferred loading retains both capability and Toolset
instructions without copying SDK implementation methods.

Leaf Toolset contributions are read after capability binding. The optional
`wrap_toolset(layer, toolset)` wraps each leaf with its complete owning Layer;
the resulting Toolset binds through native `for_run` at tool resolution. Ordinary
runs then have a finalized capability registry, while each Activity binds and
enters its own resource instance. Stateful components return independent instances
from native `for_run`; static definitions do no resource I/O. Capability factories
producing unknown runtime subtrees are outside this static composition contract.
Callable contributions and dynamic Toolsets within declared leaves remain supported.
Layer instructions have no application ordering contract. SDK hook ordering,
including the durable message recorder, still applies.

These examples use deterministic SDK models and show static assembly, injected access, explicit
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

`shell.layer.make` returns `Layer[D, Config, State]` with the shared shell models,
fixed native Toolset and explicit enter/exit hooks. Its deps contract needs only
`session_id`; injected access defines where its data lives. Create a durable session with:

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

## MCPs layer

`mcps.make` contributes a native Pydantic AI `MCPToolset` with a `RenamedToolset`
wrapper through a run-local `DynamicToolset`.
It launches the independent [MCP Gateway](../../../packages/mcp_gateway/README.md)
as a stdio child process with complete inline configuration. This private pipe
uses no JWE or downstream authentication. Upstream authentication remains in Config.
Create a session with:

```json
{
  "layers": {
    "mcps": {
      "servers": {
        "github": {"transport": "http", "url": "https://github-mcp.example.com/mcp"},
        "local": {
          "transport": "stdio",
          "command": "uvx",
          "args": ["example-mcp-server"],
          "credential_env": ["API_KEY"]
        }
      },
      "credentials": {
        "github": {"bearer": "upstream-access-token"},
        "local": {"env": {"API_KEY": "local-api-key"}}
      }
    }
  }
}
```

Upstream keys become namespaces: the Gateway exposes `github.search` and
`local.search` as distinct tools. The layer maps these to stable model aliases such
as `github_search_<digest>`, using letters, digits, underscores and dashes with a
64-character limit. A digest prevents `a.b` and `a_b` from sharing an alias; any
remaining collision fails discovery. The SDK's native renaming wrapper routes
calls back to the original MCP name and preserves schemas and tool flags.
HTTP, SSE and stdio upstreams are supported. Omitted credentials use the
upstream's fixed headers/env or anonymous access; all configured upstreams are exposed.
HTTP credentials accept bearer, basic or explicit headers. Stdio credentials may
only set names declared by `credential_env`. Custom Python auth factories require
the Gateway's Python API and cannot be serialized through this layer's CLI path.
`init_timeout` defaults to 30 seconds; `read_timeout` to 300 seconds. Each upstream
also has its own `timeout`, defaulting to 30 seconds.

Layer assembly, Config validation and session creation perform no process or network
I/O. Each selected Agent run creates and enters a fresh MCPToolset; disabled runs
create none. In ordinary runs the SDK closes the Gateway on success, error or
cancellation. In the current Temporal Runner, native discovery and tool Activities
each create and close their own stdio Gateway through the SDK. Workflow replay
performs no MCP I/O. State is always `{}` and there are no business-scope hooks or
clients saved in the session.

Config fixes the upstream set for that session; different sessions may supply
different configurations. The native MCPToolset discovers upstream tools for each
new run, then uses its discovered schema during that run. Configuration changes
require a new Gateway/session. The Gateway connects upstreams per request, so
upstream stdio processes are also closed after each request and their in-memory
state does not persist across tool calls. See the Gateway's aggregation contract
for resources, prompts and protocol forwarding; the layer supplies MCP tools to the model.

For standalone SDK consumers, `make_toolset` builds an unconnected `Toolset`
wrapper around a native client:

```python
from kapy.layers.mcps import Config, make_toolset
from pydantic_ai import Agent

config = Config.model_validate({
    "servers": {"public": {"transport": "http", "url": "http://localhost:9000/mcp"}}
})
agent = Agent("openai:gpt-4.1", toolsets=[make_toolset(config)])
# await agent.run("Use public's tools") starts and closes its Gateway.
```

`toolset.mcp` gives the native `MCPToolset` for direct calls using original MCP
names, such as `await toolset.mcp.direct_call_tool("public.search", {...})` inside
`async with toolset`. `toolset.name_map` contains model alias → original MCP name
after discovery. The registered layer and standalone builder use the same wrapper.

Kapy's Config JSON export deliberately retains plaintext upstream credentials so
PostgreSQL configuration and Temporal Activity inputs can restore them; repr hides
secret values. These credentials are part of the stored configuration and public
session DTO, like the supplied inline data. Processes, connections and JWE keys
never enter State. The independent `InlineGatewayConfig` keeps its ordinary JSON
export redacted and exposes plaintext only through its explicit startup export.

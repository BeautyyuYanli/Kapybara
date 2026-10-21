# Kapy layers

Kapy consumes the independent [Pydantic AI Layer Protocol](../../../packages/pydantic-ai-layer-protocol/README.md).
The package owns Layer definitions, registry assembly, Config/State validation,
native capability composition. The shell and MCP
implementations, installed factory mapping, scope retry policy and SessionService
remain in Kapy.

`registry.layer_registry` is a process-local `LayerRegistry` constructed
with `{"shell": shell.make, "mcps": mcps.make}` and Kapy's RunnerDeps.layers adapter once.
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
The create Worker validates Config and no-enter default States, then atomically
inserts creating with fixed Config and its actual Activity UUID/native run_id.
The default session ID is the Activity UUID. Only the executing Worker reserves
business state; hooks never hold a database transaction. Without enter,
State is initialized from `{}`; returned State can require fields without defaults.
Config and State remain JSON, while SDK component/client lifetimes stay separate.

When upstream supports resource discovery or cleanup by scope, `exit_scope` must
use the stable `scope_id` to clean that scope's resources, including resources never
recorded in saved State. Otherwise, cleanup may rely on State, but its implementation
must document that State may lag or incompletely represent upstream reality and
can therefore leave orphaned resources.

Complete State, ready and ownership release commit together. Failed creation
moves to closing while retaining its writer through compensation, exits
all configured layers in reverse with None, including partial/uncalled enters, then
finishes closed or closing and releases ownership. Temporal records failure.
None does not mean no remote resources.
An uncertain ready commit is checked before compensation; a committed success
cannot be overwritten or cleaned up by that failed acknowledgment.

Close claims unowned ready/closing, or CAS-replaces an old exact execution after
Temporal confirms terminal/NOT_FOUND, using a new UUID and native run_id.
Exit hooks run in reverse and save validated mutable State after each attempt,
including errors/cancellation. Ordinary failures still attempt remaining exits;
all cleanup must succeed for closed. Failure saves progress and closing. Same-key
terminal close returns its retained Temporal result; a new explicit close may repeat successful
exits, so exits must always be idempotent and accept None for abandoned initialization.

Only outer `RetryableScopeError` allows three hook attempts separated by 0.5 seconds.
Nested causes/groups, invalid State and ordinary errors are not retry signals.
The fixed ID/Config survives those attempts; State progress is validated and saved
before the next exit attempt. Storage/validation failure does not retry a hook.
This is the existing business-hook contract; Temporal itself uses one attempt and
never restores the Agent or automatically replays enter.

Every new scope/model/tool call checks the session's current UUID/native run_id.
Progress writes conditionally check full writer identity and lifecycle in one short
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
Saved State may lag upstream after a crash or an unacknowledged job creation, so
jobs omitted from that inventory can remain as orphaned upstream resources.

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
create none. The SDK closes the Gateway on success, error or cancellation. Kapy's
standalone Activity executes the complete Agent run in the same native SDK scope, so tool
discovery and all calls share one Gateway for that run. Later runs create fresh
processes. State is always `{}` and there are no business-scope hooks or clients
saved in the session.

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

config = Config.model_validate(
    {"servers": {"public": {"transport": "http", "url": "http://localhost:9000/mcp"}}}
)
agent = Agent("openai:gpt-4.1", toolsets=[make_toolset(config)])
# await agent.run("Use public's tools") starts and closes its Gateway.
```

`toolset.mcp` gives the native `MCPToolset` for direct calls using original MCP
names, such as `await toolset.mcp.direct_call_tool("public.search", {...})` inside
`async with toolset`. `toolset.name_map` contains model alias → original MCP name
after discovery. The registered layer and standalone builder use the same wrapper.

Kapy's Config JSON export deliberately retains plaintext upstream credentials so
PostgreSQL session configuration and create's original Temporal request retain them; repr hides
secret values. These credentials are part of the stored configuration and public
session DTO, like the supplied inline data. Processes, connections and JWE keys
never enter State. The independent `InlineGatewayConfig` keeps its ordinary JSON
export redacted and exposes plaintext only through its explicit startup export.

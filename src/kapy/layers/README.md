# Kapy layers

Kapy uses the independent [Pydantic AI Layer Protocol](../../../packages/pydantic-ai-layer-protocol/README.md)
for registry assembly, Config/State validation and native capability composition.
The process-local registry installs shell and MCPs once. It reads JSON data through
`RunnerDeps.layers` and runtime handles through `RunnerDeps.layer_resources`.

## Execution lifecycle

Session creation validates fixed Config and initializes State from `{}` locally.
State saves business progress as JSON. The Worker uses the same layer definitions
to restore both when it claims a run.

The Worker enters `layer_registry.open_conversation(str(session_id), configs)`
around each Agent Activity. A layer may supply an async
`enter_conversation(conversation_id, config) -> Resource` to open a local handle
and an async `exit_conversation(resource) -> None` to close it. Resource hooks
run outside database transactions, with successful handles unwound in reverse
order. The external service or its orchestrator manages long-lived remote entities.
The SDK separately manages native Toolsets. See [business layer rules](../../../agent_lint/layers.md).

Kapy passes `conversation_id=str(session_id)` on each Agent run, including when
resuming saved history. Tools and SDK hooks that use the identity read
`ctx.conversation_id`. Closing a Session records a PostgreSQL tombstone.

## Shell layer

Create a session with:

```json
{"layers": {"shell": {"cwd": "/workspace", "base_url": "http://shellctl:8765"}}}
```

`cwd` is an existing absolute POSIX directory on the remote server. Config also
accepts `env`, `token_env` and `redact_patterns`. The default server is
`http://127.0.0.1:8765`. The Worker resolves the named token environment variable
when opening the local HTTP client. Tools share that client and a redactor for the
run and forward only configured `env` entries.

`builtin_shell_run`, `builtin_shell_wait`, `builtin_shell_input` and
`builtin_shell_interrupt` execute sequentially. Run records the returned job ID
before reading more output. Wait and input resume from the stored byte offset.
Interrupt retains the job and its logs. Confirmed `job_not_found` removes the
entry. State is `{"jobs": {"job-id": byte_offset}}` and cursors only move forward.

Layer exit closes the HTTP client with a five-second shielded timeout. Remote jobs
and logs survive Activity exit and Session closure according to shellctl's own
retention and garbage collection. A lost response can leave a job absent from the
inventory. Output limits, tail supplementation and redaction are implemented in
`layers.shell.output`.

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
processes. State is always `{}`; the SDK owns the gateway client.

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
PostgreSQL session configuration retains them. The representation hides secret
values. These credentials are part of the stored configuration and public
session DTO, like the supplied inline data. Processes, connections and JWE keys
never enter State. The independent `InlineGatewayConfig` keeps its ordinary JSON
export redacted and exposes plaintext only through its explicit startup export.

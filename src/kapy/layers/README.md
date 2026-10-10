# Layers

A layer is an installed Python module selected by a plain `name -> module`
registry. It exports Pydantic `Config` and `State` classes and exactly one native
Pydantic AI `Capability` or `Toolset` class. The component constructor takes its
registered name, retains that name, and uses it as its stable SDK ID. There is no
configuration factory or instance stored in the registry.

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

`layers_capability(registry)` captures the module mapping at Agent construction
and installs one stable `DynamicCapability(id="layers")`. Empty deps enables no
layers. A Toolset is wrapped in a native capability; native capability hooks,
instructions, tool flags and resource lifecycles remain SDK-owned. Layer
instructions have no application ordering contract. SDK hook ordering, including
the durable message recorder, still applies.

The small Activity ownership adapter preserves `capability_id` when SDK 2.54.0
rebuilds a dynamic capability in a `TemporalRunContext` without live capabilities.
Ordinary runs use the native combined capability. Deferred capability instructions
retain native behavior; deferred Toolset instructions depend on the SDK's loader.
Capabilities should declare such instructions on the capability itself.

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
`Config`, `State`, and `Toolset`. Create a durable session with:

```json
{"layers": {"shell": {"cwd": "/workspace", "base_url": "http://shellctl:8765"}}}
```

`cwd` must be an absolute POSIX directory that already exists on the remote
server. Config also accepts `env`, optional `token_env`, and `redact_patterns`.
The default server is `http://127.0.0.1:8765`. The Worker reads the named token
environment variable only during an actual tool call or session cleanup.
State is `{"jobs": {"job-id": byte_offset}}`; it contains no clients or secrets.

The tools retain the existing names and parameters: `builtin_shell_run`,
`builtin_shell_wait`, `builtin_shell_input`, and `builtin_shell_interrupt`.
Run records a returned job ID before reading output. Wait/input resume from its
saved byte cursor; cursors never decrease. Interrupt retains the job and its
readable logs. Confirmed `job_not_found` removes the entry. Output limits,
truncation, tail supplementation and redaction are shared with the legacy shell
plugin in `agent_plugins.builtin.shell`.

Each tool owns and closes its HTTP client, with bounded shielded cleanup. The
remote job survives client close and successful runner completion. Session close
deletes known jobs one by one, recording each confirmed deletion. Failed cleanup
leaves CLOSING and remaining jobs for an explicit close retry. It owns neither the
shellctl server nor its directories and files. Ambiguous remote acknowledgements
can leave an unrecorded job or an already applied input; there is no remote
exactly-once protocol.

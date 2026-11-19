# Pydantic AI Layer Protocol

A standalone Python package for typed layer definitions, registry assembly and
native Pydantic AI capability composition. It supports Python 3.12 and later.
Applications own installed layers, conversation lifecycles and storage; this package has
no application, database, shell service or credential dependency.

Install from this repository:

```sh
pip install ./packages/pydantic-ai-layer-protocol
```

The import name is `pydantic_ai_layer_protocol`. The package requires no Temporal SDK.

## Definitions and registry

A `Layer[D, Config, State, Resource]` is a static definition containing two
Pydantic model classes, one native `AbstractCapability[D]` and optional hooks
for a local runtime handle. A factory accepts `(layer_id: str, access: LayerAccess[D])`
and returns that definition. Use `None` as the fourth type argument for a layer
without a handle. D is opaque to layers. Config and State are bounded by
`BaseModel`. The fourth argument ties the handle returned by enter to the exit hook.

`LayerRegistry(factories, get_layers=get_layers)` assembles every installed factory once,
without execution deps. It checks the returned Layer, model classes, native
capability, async hooks and root capability ID. The root ID must equal the
registered nonempty string; the framework never rewrites it. Function tools inherit
the Capability ID during SDK construction. Nested leaves and internal Toolsets
may use independent or derived stable IDs; every registered capability leaf must
have one, including leaves without tools. IDs and composition membership stay
fixed after construction. Native Combined containers do not need extra IDs.

The caller supplies `get_layers(deps) -> dict[str, LayerData]`. Its mapping is the
current enabled selection, separate from the installed factory allowlist. Each
injected `access.data(deps)` returns the writable entry for its own ID.
Applications provide the installed factory mapping and configure the deps layout.
Changing the installation set requires reassembly.

The registry preserves inferred deps type D across the access rule, factories,
definitions and native capability. Install this object directly in an Agent.
Its read-only `definitions` mapping exposes model types, capabilities and resource
hooks. The host explicitly opens the selected handles around its Agent runs.

Each entry is a JSON `config` object and `state` object. Config stays fixed for
the session. Models read from current data are disposable copies; mutation requires
an explicit writeback, which updates deps but does not itself save to a database:

```python
from pydantic import BaseModel
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import Capability
from pydantic_ai_layer_protocol import Layer, LayerAccess, LayerData, LayerRegistry, dump_object


class Deps(BaseModel):
    layer_data: dict[str, LayerData]


class Config(BaseModel):
    increment: int = 1


class State(BaseModel):
    calls: int = 0


def make_counter(layer_id: str, access: LayerAccess[Deps]) -> Layer[Deps, Config, State, None]:
    def count(ctx: RunContext[Deps]) -> int:
        entry = access.data(ctx.deps)
        config = Config.model_validate(entry["config"])
        state = State.model_validate(entry["state"])
        state.calls += config.increment
        entry["state"] = dump_object(state)
        return state.calls

    return Layer(
        config_type=Config, state_type=State, capability=Capability(id=layer_id, tools=[count])
    )


def get_layers(deps: Deps) -> dict[str, LayerData]:
    return deps.layer_data


layers = LayerRegistry({"counter": make_counter}, get_layers=get_layers)
agent = Agent(..., deps_type=Deps, capabilities=[layers])
```

Factories, access rules, validators and instruction callbacks are deterministic
and perform no external I/O. Static definitions never retain a session's deps,
models, conversation ID or clients. Tools (collection, names and argument schemas) and
instructions stay stable for fixed Config. SDK callbacks may render those fixed
declarations; dynamic business information belongs in State and tool results,
so it does not change the model prefix.

`layers.load(configs, states)` validates and copies existing JSON. Every State
model can initialize from `{}` when its entry is omitted. Unconfigured State and
unknown names fail validation. `layers.snapshot(deps)` validates and copies selected
States. Both models serialize to JSON objects. Validation receives detached inputs
and runs without resource hooks.


## Native SDK composition

The registry is a thin native `WrapperCapability` over a private selector. For
each run it validates current Config/State and selects enabled definitions in the
deps mapping's order. Unknown names, invalid data and missing handles for selected
layers fail before capability execution.

The original static capabilities are selected directly through native
`CombinedCapability.for_run`; native `for_agent` performs Agent binding. Missing
leaf IDs fail during registry construction. Wrappers and Combined containers retain
their native structure and declared ordering. Native Combined owns composition,
instructions, hooks, deferral and tool ownership.

Leaf Toolset contributions are read after capability binding and bind through
native `for_run` at tool resolution, with the finalized capability registry.
Stateful components return independent instances from `for_run`; static definitions
perform no resource I/O. Callable contributions and dynamic Toolsets within declared
leaves remain supported. Capability factories producing unknown runtime subtrees
are outside this static composition contract. Layer instructions have no application
ordering contract. Tools sharing mutable State must explicitly declare sequential
behavior when required by their business semantics. Hosts own final persistence.

## Conversations

A Layer can supply these async hooks:

```python
async def enter_conversation(conversation_id: str, config: Config) -> Resource: ...
async def exit_conversation(resource: Resource) -> None: ...
```

Enter turns a Config locator into a local runtime handle. It cleans up any
partial initialization if it fails. Exit closes or detaches the returned handle.
The external service or its orchestrator manages the lifetime of any referenced
remote entity. An enter-only layer can return an in-memory handle that needs no
cleanup. Exit requires enter, and installing layers with enter requires a
`get_resources(deps)` adapter on the registry.

`async with layers.open_conversation(conversation_id, configs) as resources`
validates all Config and the nonempty string ID before entering selected layers in
configuration order. Successful enters are unwound in reverse order, including
after a failed enter. The hooks and the body share the caller's task and context,
so exit hooks can unwind AnyIO cancel scopes and task groups opened by enter
hooks. The registry shields cleanup from enclosing AnyIO cancel scopes and
delivers their cancellation after cleanup. Direct `asyncio.Task.cancel()` calls
can still interrupt a suspended exit hook.

Use the yielded mapping in deps for each run inside that context.
`access.resource(ctx.deps)` returns the local handle for the layer and raises
`UserError` for a missing handle. For the provided `LayerDeps`, use
`get_resources=lambda deps: deps.resources`. Its JSON serialization contains only
layer data.

Use the same ID in `agent.run(..., conversation_id=conversation_id)` for each
run. Tools and SDK hooks can read it from `ctx.conversation_id`. The host may
reuse open handles across runs and owns final State persistence.

## Examples and checks

```sh
python -m pydantic_ai_layer_protocol.examples.in_process
python -m pytest -q tests
```

Examples use deterministic SDK models. Tests cover native Agent lifecycle and
composition without requiring a Temporal service.

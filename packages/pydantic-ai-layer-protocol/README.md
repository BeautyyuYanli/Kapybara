# Pydantic AI Layer Protocol

A standalone Python package for typed layer definitions, registry assembly and
native Pydantic AI capability composition. It supports Python 3.12 and later.
Applications own installed layers, scope lifecycles and storage; this package has
no application, database, shell service or credential dependency.

Install from this repository:

```sh
pip install ./packages/pydantic-ai-layer-protocol
pip install './packages/pydantic-ai-layer-protocol[temporal]'
```

The import name is `pydantic_ai_layer_protocol`. The core package requires no
Temporal SDK; optional State transport lives in `pydantic_ai_layer_protocol.temporal`.

## Definitions and registry

A `Layer[D, Config, State]` is a static definition containing Pydantic model
classes, one native `AbstractCapability[D]` instance and two independently
optional business-scope hooks. An installed factory has the signature
`make(layer_id: str, access: LayerAccess[D]) -> Layer[D, Config, State]`.
The concrete return annotation links the hook parameters and asynchronous return
values to the models; exit must accept `State | None`.
D is unbounded and opaque to the layer. Only C/S are bounded by BaseModel;
all layer business data comes from injected access and those models.

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
injected `access(deps)` returns the actual writable entry for its own ID, without
copying it or creating missing entries. Each application owns its installed factory mapping and its deps layout.
Changing the installation set requires reassembly.

The registry preserves inferred deps type D across the access rule, factories,
definitions and native capability. Install this object directly in an Agent.
Its read-only `definitions` mapping exposes model types and business-scope hooks;
the registry owns selection and JSON validation while the host drives those hooks.

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


def make_counter(layer_id: str, access: LayerAccess[Deps]) -> Layer[Deps, Config, State]:
    def count(ctx: RunContext[Deps]) -> int:
        entry = access(ctx.deps)
        config = Config.model_validate(entry["config"])
        state = State.model_validate(entry["state"])
        state.calls += config.increment
        access(ctx.deps)["state"] = dump_object(state)
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
models, scope ID or clients. Tools (collection, names and argument schemas) and
instructions stay stable for fixed Config. SDK callbacks may render those fixed
declarations; dynamic business information belongs in State and tool results,
so it does not change the model prefix.

`layers.prepare_configs(configs)` validates all names and Config before
scope hooks, without constructing State. `layers.load(configs, states)`
validates and copies existing JSON; omitted State validates from `{}` and fails
when required fields are missing. It calls no scope hooks. Unconfigured State and
unknown names fail. `layers.snapshot(deps)` uses the saved access rule to validate and copy
only the selected States. Both models must serialize to JSON objects; inputs are
copied before validation so custom validators cannot mutate caller-owned data.


## Native SDK composition

The registry is a thin native `WrapperCapability` over a private selector.
At Agent construction it exposes all installed entries so the SDK can register
Activities. For each run it validates current Config/State and selects enabled
definitions in the deps mapping's order; an empty selection enables zero layers.
Unknown names or invalid data fail before capability execution. Factories are not
rerun. Each registered leaf is wrapped in a native `DynamicCapability` whose
Activity ID equals that leaf's stable capability ID. Missing leaf IDs fail during
registry construction. A Wrapper over one leaf is wrapped as a whole; Combined
containers and Wrappers containing children retain their native structure.

Native Combined owns composition, instructions, hook ordering and tool ownership
in ordinary runs and on the Temporal Workflow side. Native Dynamic rebuilds each
leaf inside Activities, which do not receive the live capability registry.
Successful discovery returns tool definitions for native Workflow-side ownership
and deferral processing. Deferred loading retains both capability and Toolset
instructions without copying SDK implementation methods.

Leaf Toolset contributions are read after capability binding and bind through
native `for_run` at tool resolution. Ordinary
runs then have a finalized capability registry, while each Activity binds and
enters its own resource instance. Stateful components return independent instances
from native `for_run`; static definitions do no resource I/O. Capability factories
producing unknown runtime subtrees are outside this static composition contract.
Callable contributions and dynamic Toolsets within declared leaves remain supported.
Layer instructions have no application ordering contract. SDK hook ordering,
including the durable message recorder, still applies.


## Business scopes

A Layer can explicitly supply either or both async hooks:

```python
async def enter_scope(scope_id: UUID, config: Config) -> State: ...
async def exit_scope(scope_id: UUID, config: Config, state: State | None) -> None: ...
```

The host chooses the UUID and owns initialization, cleanup, persistence and retry
scheduling. Assembly, selection, load/snapshot and SDK component lifecycles do not
invoke these hooks. Their validated model arguments remain separate from execution
deps. Enter may return State with required fields that cannot initialize from `{}`.

Exit must be idempotent and clean partial or unacknowledged initialization using
scope ID and Config even when State is None. None does not mean there are no
resources. Enter need not be generally idempotent; a top-level RetryableScopeError
explicitly promises that repeating this call with the same identity and Config is
safe. The host chooses retry limits. Nested causes, exception groups and cancellation
do not imply retryability. The protocol provides no storage, leases or automatic
resource recovery.

## Optional Temporal State transport

Temporal serializes Activity deps independently, so Activity mutation does not
update Workflow deps. Import the optional adapter explicitly:

```python
from pydantic_ai import Agent
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai_layer_protocol.temporal import TemporalLayerRegistry

layers = TemporalLayerRegistry({"counter": make_counter}, get_layers=get_layers)
agent = Agent(..., deps_type=Deps, capabilities=[layers, TemporalDurability()])
```

This registry assembles the selector and both transport directions together using
one access rule. Consumers supply no Toolset wrapper callback or separate State
capability. Use the core `LayerRegistry` for ordinary runs; both expose the same
`definitions`, `prepare_configs`, `load` and `snapshot` interfaces.

The inner Toolset validates the called layer's State and carries it in
ToolReturn.metadata. The Workflow wrapper applies only that layer's State before
business hooks and restores the original value or native ToolReturn, including
content, metadata and tools. Layer tools execute sequentially, so subsequent
Activities receive the preceding returned State. Failures, cancellation and native
ModelRetry bypass the success transport; the adapter performs no database I/O.

LayerStateMetadata requires `state`, a strict boolean `native_return`, and arbitrary
original `metadata`, and forbids unknown fields. Tool definitions identify their
State owner under `LAYER_STATE_OWNER_KEY` (`pydantic_ai_layer`), which is reserved
for this adapter. In ordinary Agent runs the transport wrappers pass results
through and tools mutate the caller's
writable deps directly. Consumers still own final persistence and failure semantics.

## Examples and checks

```sh
python -m pydantic_ai_layer_protocol.examples.in_process
python -m pydantic_ai_layer_protocol.examples.temporal
```

Examples use deterministic SDK models. The Temporal example needs a local Temporal
service and uses the entry above for composition and State transport.

From this package directory, core checks need only the dev dependencies:

```sh
python -m pytest -q tests/test_registry.py tests/test_types.py
```

`tests/test_temporal.py` additionally uses the Temporal extra and a real service;
TEMPORAL_ADDRESS defaults to localhost:7233 and TEMPORAL_NAMESPACE to default. Test
capabilities own their resources and use unique Workflow IDs and task queues.

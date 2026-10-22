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

A `Layer[D, Config, State]` is a static definition containing Pydantic model
classes, one native `AbstractCapability[D]` instance and two independently
optional conversation hooks. An installed factory has the signature
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
models, conversation ID or clients. Tools (collection, names and argument schemas) and
instructions stay stable for fixed Config. SDK callbacks may render those fixed
declarations; dynamic business information belongs in State and tool results,
so it does not change the model prefix.

`layers.prepare_configs(configs)` validates all names and Config before
conversation hooks, without constructing State. `layers.load(configs, states)`
validates and copies existing JSON; omitted State validates from `{}` and fails
when required fields are missing. It calls no conversation hooks. Unconfigured State and
unknown names fail. `layers.snapshot(deps)` uses the saved access rule to validate and copy
only the selected States. Both models must serialize to JSON objects; inputs are
copied before validation so custom validators cannot mutate caller-owned data.


## Native SDK composition

The registry is a thin native `WrapperCapability` over a private selector. For
each run it validates current Config/State and selects enabled definitions in the
deps mapping's order; an empty selection enables zero layers. Unknown names or
invalid data fail before capability execution. Factories are not rerun.

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

A Layer can explicitly supply either or both async hooks:

```python
async def enter_conversation(conversation_id: str, config: Config) -> State: ...
async def exit_conversation(conversation_id: str, config: Config, state: State | None) -> None: ...
```

The host chooses the string ID and owns initialization, cleanup, persistence and retry
scheduling. Assembly, selection, load/snapshot and SDK component lifecycles do not
invoke these hooks. Their validated model arguments remain separate from execution
deps. Enter may return State with required fields that cannot initialize from `{}`.
Without enter, the host initializes State from `{}`. Config validation, default
State initialization and serialization perform no external I/O; enter may do business I/O.
The protocol does not require UUIDs and provides no aliases for the old hook names.

Pass the same ID explicitly to `agent.run(..., conversation_id=conversation_id)`
on every run and to these independent callbacks outside the run. SDK tools and
hooks read `ctx.conversation_id`; they still use injected access for Config/State
without depending on application-specific identity fields in deps. State stores
business progress and necessary cleanup references, rather than conversation identity.

Resource-creating tools/hooks must mark upstream resources or their scope with
conversation ownership when supported. Determine or derive the marker from the
conversation ID, layer ID and fixed Config as needed, using only information
necessary to locate that conversation's resources; the model must not choose or
override it. Exit must reconstruct the marker and reclaim all owned resources,
including resources missing from State. If upstream cannot query or clean by
ownership, State-based cleanup is best effort and must document possible orphans.

Exit must be idempotent and clean partial or unacknowledged initialization using
conversation ID and Config even when State is None. None does not mean there are no
resources. Enter need not be generally idempotent; a top-level RetryableScopeError
explicitly promises that repeating this call with the same identity and Config is
safe. The host chooses retry limits. Nested causes, exception groups and cancellation
do not imply retryability. The protocol provides no storage, leases or automatic
resource recovery.

## Examples and checks

```sh
python -m pydantic_ai_layer_protocol.examples.in_process
python -m pytest -q tests/test_registry.py tests/test_types.py
```

Examples use deterministic SDK models. Tests cover native Agent lifecycle and
composition without requiring a Temporal service.

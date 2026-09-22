# Control services

`ModelService` manages local provider/model configuration. `SessionService` is the
user-side entry point for session configuration, inputs, cancellation, output and
execution. The agent runner remains independent: it accepts an Agent, values and
callbacks, and owns only the lease, history, checkpoint and context paging flow.

Import both services before creating `ControlTable.metadata` tables; application
code owns database creation/migrations, engines and session factories. All control
tables use the connection's default schema, generic JSON and application-checked
references without physical foreign keys. The runner's execution repository still
requires PostgreSQL row locking and database-clock lease checks.

Application commands initialize the core database with `kapy db upgrade`; see
[database migrations](../database/README.md). The following in-process example
assumes that migration has already run:

```python
from pydantic import SecretStr
from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kapy.control.models import CreateModel, CreateProvider, ModelService
from kapy.control.sessions import CreateSession, SessionService


async def example(database_url: str, api_key: str, model_name: str):
    engine = create_async_engine(database_url)
    try:
        factory = async_sessionmaker(engine, expire_on_commit=False)
        models = ModelService(factory)
        sessions = SessionService(factory, heartbeat_interval=10, heartbeat_timeout=60)
        provider = await models.create_provider(
            CreateProvider(
                name="OpenAI",
                provider_class="pydantic_ai.providers.openai:OpenAIProvider",
                model_class="pydantic_ai.models.openai:OpenAIResponsesModel",
                api_key=SecretStr(api_key),
            )
        )
        model = await models.create_model(
            CreateModel(
                provider_id=provider.id,
                model_name=model_name,
            )
        )
        session = await sessions.create_session(
            CreateSession(
                provider_id=provider.id,
                model_name=model.model_name,
                compaction_threshold_tokens=20_000,
            )
        )
        await sessions.enqueue_input(session.id, "queued", "Explain this project.")
        agent = Agent(instructions="Be concise and precise.")
        return await sessions.start_runner(session.id, agent=agent)
    finally:
        await engine.dispose()
```

Providers have UUID identities and immutable importable Provider/Model class
references. The supported model protocols are OpenAIChatModel,
OpenAIResponsesModel and GoogleModel, including compatible subclasses. Provider
construction passes the stored api_key, optional base_url and JSON provider_kwargs
directly to its SDK constructor. Class references select installed code: they do
not translate incompatible SDK protocols. ProviderRecord returns provider_kwargs unchanged as ordinary constructor configuration.
Only api_key is excluded from ordinary results; actual credentials are stored in the database;
SecretStr only controls their Python representation. Client injection parameters
are not accepted as stored configuration.

Models use `(provider_id, model_name)` as their identity, with a separate display
name, JSON request settings and nullable context_window. Google names are
normalized by removing `models/`; use the returned model_name in later operations.
An omitted/None capacity on creation is inferred from the SDK's native profile
without making a model request. Unknown capacity stays None. Updating capacity to
None clears it without inference. No profile object is persisted.

`discover_models(provider_id)` traverses the SDK's model-list pages and atomically
imports missing models, using the same SDK capacity inference. It preserves all
existing settings, names, capacity values and timestamps, and never removes absent
remote models. Google discovery includes only generateContent models. Unsupported
listing and remote failures raise ModelDiscoveryError; manual creation is still
available. Provider/model get and update operations raise LookupError for missing
records; delete succeeds idempotently if the resource is already absent.
ModelAlreadyExists reports duplicate model identity. Session model references are
weak: create/update may retain unresolved identifiers, and deleting a provider
deletes its local models without blocking on or rewriting session references.
No remote resources are deleted.

Sessions have UUID identities, title, model identity, JSON model_settings and two
paging columns, context_plugin JSON and a ready/closing/closed lifecycle status. create/get/list/update
and close APIs retain records; there is no physical deletion. Fixed plugin bindings
are supplied in CreateSession.plugins. The shared application factory collects
business capabilities and creates a fully configured base Agent per execution;
direct Agent calls remain available for sessions without plugins. DTOs reject
unknown fields. Updates preserve omitted fields and replace supplied JSON
objects as a whole; `{}` clears a preset/override. Explicit None is accepted only
for provider base_url, model context_window and session compaction_threshold_tokens.
Model switches must supply both provider_id and model_name. Lists use stable
creation-time/identity order with offset >= 0 and limit between 1 and 200, returning
`Page(items, has_more)`. `read_history(id, before_seq=None, limit=100)` returns the
latest matching page in ascending seq order; before_seq is exclusive and has_more
means older history exists. History and ordinary lists share kapy.pagination.


Session creation validates plugin config before saving ready session/binding records
in one transaction; it does not allocate external resources. `close_session(id)`
records closing, requests cooperative runner cancellation, then closes bindings
sequentially. Failure preserves progress; retry skips closed bindings. It uses no
runner lease and does not wait for in-flight external work. Only registered cleanup
blocks closed; late orphan resources require plugin reconciliation. See
[Agent plugins](../agent_plugins/README.md) for StateStore and resource contracts.

Input intake, runner acquisition/resumption, subsequent input consumption and new
tools require ready. Queries/history/live, cancellation, input withdrawal and
ordinary configuration updates remain usable in closing/closed. SDK-dependent
model settings are validated at execution startup, leaving inputs untouched on
failure. Plugin choices and lifecycle fields cannot be patched through UpdateSession.

At startup, SessionService reads session/model/provider configuration once in a
short transaction, then releases it before constructing SDK resources. It merges
model.settings with session.model_settings at the top level, validates against the
SDK's protocol Settings type, and delegates that snapshot to the application
execution factory. The factory opens Provider/Model and plugin resources inside
the lease, creates the Agent with its model/settings, and collects SessionReady,
plugin capabilities and paging configuration into RunnerExecution. The runner
installs capabilities when opening the SDK graph. The service does not wrap the
factory's Agent or append business capabilities. Direct caller-owned Agents use
a separate adapter with task-local model/settings overrides, retaining their
prompts, tools, deps and output type. The same configuration values remain active
through every queued run, lease reacquisition and temporary compaction;
configuration updates affect the next start. Explicit SDK options retain their
SDK semantics; no extra runner-specific settings blacklist is applied.

A configured SessionExecutionFactory receives the service, a validated
SessionExecutionConfig snapshot and the service's ContextPluginRegistry, and yields
RunnerExecution within its resource context. The default implementation is
application.agent.create_execution_factory; application.sessions injects it into
both interfaces. No model or plugin resource exists before lease acquisition.

`SessionService(..., context_plugin_registry=...)` selects an implementation from the
session's `context_plugin: {"name": "kapy/summary", "config": {}}`. The name is fixed
on creation. PATCH replaces the supplied config object completely and cannot change
the name. Constructors validate plugin configuration inside each acquired execution,
before input consumption. Unknown names or invalid config fail without draining inputs.
All configuration is snapshotted for a start call; edits apply on the next start.
HTTP and Telegram share `application.sessions.create_session_service`.

The host owns pagination independently of plugin config. A positive
compaction_threshold_tokens enables paging; null resolves to 70% of model capacity
at startup. Creation stores 183500 when capacity is unknown and no threshold is
provided; updates do not apply this default. A stored null with unknown capacity
fails startup. compaction_replay_turns is nonnegative, defaults to 10, and controls
the prior original rounds supplied as plugin reference. Only the summary plugin
chooses to replay these in its returned upper context. Lower-level callers inject
context_plugin plus separate host threshold/reference options; see the
[paging contracts](../agent_runner/README.md).


Heartbeat interval and timeout are finite constructor settings satisfying
`0 < interval < timeout`. Every worker using the same session lease table must use the
same policy, including callers that bypass SessionService. is_runner_running uses
this timeout and the database clock to observe a non-expired owned lease, even if
the checkpoint is done. The owner may be a non-runner operation; this observation
does not mean the Agent is generating. It does not acquire execution or prove process liveness;
start_runner still atomically acquires the lease and raises SessionBusy if occupied.

Input is enqueued with `enqueue_input(id, "queued", content)` for the next run, or
`"steer"` to supplement the current run at its next input boundary. Intake and
consumption lock/check ready in the same transaction as their queue changes. Enqueue does
not launch a runner. `read_inputs` returns a FIFO snapshot without consuming it and
without requiring a business session row. `submit_input(id, SubmitInput(...))`
commits input first, then returns InputSubmission(input, should_start_runner); this
is only a lease observation, not execution ownership. The caller schedules work.
`delete_input(id, input_id)` returns whether it withdrew a still-pending input.
It races with checkpoint consumption through the same queue row: True means the
input cannot enter a later checkpoint, False means no matching pending row remained.
Consumption uses DELETE RETURNING, so only actually consumed input enters history
and the SDK request. Queue deletion and checkpoint commit remain atomic.
Read-only inputs/history/live/cancel/lease queries do not precheck business sessions;
get/update/start read the configuration they require, and enqueue/request_cancel
validate their own references. Empty queue/history/cancel/lease queries return
empty results or False. Full AgentState and its lease internals stay in the runner. SessionService
never keeps a database transaction open across model/tool work or output iteration.
SDK Provider and Model contexts belong to each execution: they open after lease
acquisition and close after graph and plugin cleanup, before lease release.

After each lower runner returns and releases its lease, SessionService checks both
input channels again. Pending input triggers reacquisition with fresh execution
resources using the same configuration snapshot and publisher; losing this later
race returns the preceding result, whereas an initial SessionBusy propagates.
Execution/cleanup failures are not retried.
This closes the final queue-check/release handoff window without a persistent job
queue. Cancel ends the current run at a boundary and leaves queued inputs intact.

`live(id, after_seq=-1)` owns a confirmed Pub/Sub subscription before replaying
history and following complete messages/deltas in nonempty event lists. Consumers
apply each batch in order; only complete messages advance the cursor. History
pagination uses before_seq independently. Use aclosing when
stopping early. See [HTTP adapters](../interfaces/http/README.md) for application wiring,
background scheduling and WebSocket lifetime.

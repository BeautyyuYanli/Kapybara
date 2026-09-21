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
ModelAlreadyExists reports duplicate model identity, and ResourceInUse rejects
deletion of configuration referenced by sessions. Deleting an unused provider
deletes its local models in the same transaction, without deleting remote resources.

Sessions have UUID identities, title, model identity, JSON model_settings and two
compaction columns. They have create/get/list/update APIs and no deletion API.
Prompt, tools, dependencies and output type stay on the caller-owned Agent. DTOs
reject unknown fields. Updates preserve omitted fields and replace supplied JSON
objects as a whole; `{}` clears a preset/override. Explicit None is accepted only
for provider base_url, model context_window and session compaction_threshold_tokens.
Model switches must supply both provider_id and model_name. Lists use stable
creation-time/identity order with offset >= 0 and limit between 1 and 200, returning
`Page(items, has_more)`. `read_history(id, before_seq=None, limit=100)` returns the
latest matching page in ascending seq order; before_seq is exclusive and has_more
means older history exists. History and ordinary lists share kapy.pagination.

At startup, SessionService reads session/model/provider configuration once in a
short transaction, then releases it before constructing SDK resources. It merges
model.settings with session.model_settings at the top level, validates against the
SDK's protocol Settings type, and uses Agent.override for the model and settings.
The same values remain active through every queued run and temporary compaction;
configuration updates affect the next start. Explicit SDK options retain their
SDK semantics; no extra runner-specific settings blacklist is applied.

`SessionService(..., context_policy_factory=...)` optionally supplies a pure factory
`(session_record, model_record, agent) -> ContextPolicy`. It runs outside the
configuration transaction once per start call, before clients/leases are opened, and
its policy is shared across queued and reacquired runners. Model metadata includes
context_window. The default constructs summary/v1 using the resolved threshold,
replay count and borrowed Agent/deps; custom factories need not interpret summary
fields. HTTP and Telegram both use `application.sessions.create_session_service`.
The runner core deals only with generic page state and does not import the summary
strategy. See the [paging contracts](../agent_runner/README.md).

The stored compaction_threshold_tokens must be positive or None. The default
summary factory resolves None to 70% of model capacity at startup (rounded down,
at least one). On session creation,
an omitted/None threshold stays None when model capacity is known; if capacity is
unknown, SessionService stores `256 * 1024 * 7 // 10 = 183500`. Explicit thresholds
are preserved. Updates can clear the threshold to None without applying this
creation default, and existing sessions are not backfilled. With the default summary
factory, unknown capacity plus a stored None prevents startup before acquiring
execution or consuming inputs. A custom factory may ignore these summary fields.
compaction_replay_turns is a nonnegative integer, defaults to 10, and zero omits
replay in the replaceable prefix. These are session fields, not arguments to
SessionService.start_runner. Lower-level callers pass
`context_policy=summary_context_policy(agent, threshold_tokens=..., replay_turns=..., max_retries=...)`
to open_runner or start_runner; run and rebuild_context have no compaction arguments.

Heartbeat interval and timeout are finite constructor settings satisfying
`0 < interval < timeout`. Every worker using the same session lease table must use the
same policy, including callers that bypass SessionService. is_runner_running uses
this timeout and the database clock to observe a non-expired owned lease, even if
the checkpoint is done. The owner may be a non-runner operation; this observation
does not mean the Agent is generating. It does not acquire execution or prove process liveness;
start_runner still atomically acquires the lease and raises SessionBusy if occupied.

Input is enqueued with `enqueue_input(id, "queued", content)` for the next run, or
`"steer"` to supplement the current run at its next input boundary. Enqueue does
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
SDK Provider and Model contexts cover the complete startup call and close their
owned clients after the runner and output publisher exit.

After each lower runner returns and releases its lease, SessionService checks both
input channels again. Pending input triggers reacquisition under the same SDK and
publisher contexts; losing this later race returns the preceding result, whereas
an initial SessionBusy propagates. Execution/cleanup failures are not retried.
This closes the final queue-check/release handoff window without a persistent job
queue. Cancel ends the current run at a boundary and leaves queued inputs intact.

`live(id, after_seq=-1)` owns a confirmed Pub/Sub subscription before replaying
history and following complete messages/deltas in nonempty event lists. Consumers
apply each batch in order; only complete messages advance the cursor. History
pagination uses before_seq independently. Use aclosing when
stopping early. See [HTTP adapters](../interfaces/http/README.md) for application wiring,
background scheduling and WebSocket lifetime.

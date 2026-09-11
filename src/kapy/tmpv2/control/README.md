# Control services

`ModelService` manages local provider/model configuration. `SessionService` is the
user-side entry point for session configuration, inputs, cancellation, output and
execution. The agent runner remains independent: it accepts an Agent, values and
callbacks, and owns only the lease, history, checkpoint and compaction flow.

Import both services before creating `ControlTable.metadata` tables; application
code owns database creation/migrations, engines and session factories. All control
tables use the connection's default schema, generic JSON and application-checked
references without physical foreign keys. The runner's execution repository still
requires PostgreSQL row locking and database-clock lease checks.

For a fresh application database:

```python
from pydantic import SecretStr
from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kapy.tmpv2.agent_runner.models import agent_metadata
from kapy.tmpv2.control.database import ControlTable
from kapy.tmpv2.control.models import CreateModel, CreateProvider, ModelService
from kapy.tmpv2.control.sessions import CreateSession, SessionService


async def example(database_url: str, api_key: str, model_name: str):
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(ControlTable.metadata.create_all)
            await connection.run_sync(agent_metadata.create_all)
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
not translate incompatible SDK protocols. Credentials and kwargs are excluded from
ordinary ProviderRecord results, but actual credentials are stored in the database;
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
creation-time/identity order with offset >= 0 and limit between 1 and 200.

At startup, SessionService reads session/model/provider configuration once in a
short transaction, then releases it before constructing SDK resources. It merges
model.settings with session.model_settings at the top level, validates against the
SDK's protocol Settings type, and uses Agent.override for the model and settings.
The same values remain active through every queued run and temporary compaction;
configuration updates affect the next start. Explicit SDK options retain their
SDK semantics; no extra runner-specific settings blacklist is applied.

The stored compaction_threshold_tokens must be positive, or None to use 70% of
model capacity (rounded down, at least one). Unknown capacity plus None prevents
startup before acquiring execution or consuming inputs. compaction_replay_turns is
a nonnegative integer, defaults to 10, and zero omits replay before the summary
anchor. These are session fields, not arguments to SessionService.start_runner.
The lower runner still accepts ordinary explicit compaction arguments.

Heartbeat interval and timeout are finite constructor settings satisfying
`0 < interval < timeout`. Every worker using the same execution table must use the
same policy, including callers that bypass SessionService. is_runner_running uses
this timeout and the database clock to observe a non-expired owned lease, even if
the checkpoint is done. It does not acquire execution or prove process liveness;
start_runner still atomically acquires the lease and raises SessionBusy if occupied.

Input is enqueued with `enqueue_input(id, "queued", content)` for the next run, or
`"steer"` to supplement the current run at its next input boundary. Enqueue does
not launch a runner. `read_inputs` returns a FIFO snapshot without consuming it and
without requiring a business session row. Other public session operations check
that the session exists. Runner-side consume callbacks borrow its fenced checkpoint
transaction, transferring the accepted snapshot to history atomically. SessionService
never keeps a database transaction open across model/tool work or output iteration.
SDK Provider and Model contexts cover the complete startup call and close their
owned clients after the runner and output publisher exit.

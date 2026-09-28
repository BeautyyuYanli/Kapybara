# Temporal Agent runner

`RunnerWorkflow.run(RunnerInput)` executes one fresh `agent.run()` and returns its
string output. It does not read session/model tables or acquire a lease. Callers
resolve provider/model configuration and merge and validate settings before
constructing the input:

```python
RunnerInput(
    user_prompt="Hello",
    config=DurableExecutionConfig(
        provider_class="pydantic_ai.providers.openai:OpenAIProvider",
        model_class="pydantic_ai.models.openai:OpenAIResponsesModel",
        model_name="gpt-4o-mini",
        api_key="...",
        model_settings={"temperature": 0.2},
    ),
)
```

The module defines its own Agent at module scope. It reuses Provider/Model
construction helpers, but does not import the legacy application Agent factory.

Module passthrough is centralized in the package exports: shared model helpers
transitively import SQLModel definitions that cannot be reinitialized in the
sandbox. Model class resolution also passes imports through because dynamically
loading Google SDK dependencies there otherwise triggers restricted environment
access. The Workflow module itself uses normal imports; sandboxing remains enabled.

Class references select installed trusted code, just as existing model
configuration does. The DTO contains the actual API key: Temporal persists it in
Workflow and Activity payloads. `repr=False` only suppresses repr output; payload
protection and access control belong to the deployment.

`python -m kapy.runner_duarable.worker` starts the independent Worker for both
Workflow and Activity tasks. It uses `PydanticAIPlugin()` on its Client and
`AgentPlugin(agent)` on its Worker. These are SDK registration plugins, not Kapy
business plugins. SIGTERM and SIGINT shut down the Worker; running Activities
receive up to 15 seconds to finish before cancellation.

The Worker uses the same `open_resources()` factory as the interfaces, owning
its own PostgreSQL pool, Valkey client, and Temporal Client. Its resource scope
encloses the Worker so Activities finish before resource cleanup. Database and
Valkey connections are lazy; these process-local objects must not enter Workflow
inputs or state. Activity resource injection is separate from this entry point.

The Worker and interface processes share `KAPY_TEMPORAL_ADDRESS` (default
`localhost:7233`), `KAPY_TEMPORAL_NAMESPACE` (`default`), and
`KAPY_TEMPORAL_TASK_QUEUE` (`kapy-runner`). Applications create one Temporal Client
per `open_resources()` lifespan, available as `resources.temporal_client`; HTTP
also exposes it through `request.app.state.resources.temporal_client`. Startup
requires a reachable Temporal service. Client has no explicit close API; process
owners finish their tasks before releasing resources. Session execution is not
automatically switched to this Workflow.

The resolver uses the existing Provider/Model builders to construct the real SDK
model and closes these initial contexts before returning it. Workflow code uses
its protocol-specific profile and request preparation. The SDK subsequently
re-enters and closes model contexts on both the Workflow and Activity sides;
providers recreate their owned HTTP clients on re-entry. The Workflow-side
context remains open while waiting for the model Activity, but HTTP requests
execute only inside Activities. This relies on the Provider re-entry contract:
custom configured providers must support it, and
constructors/profile preparation must not perform external I/O. No requests are
made from Workflow code. Client construction also occurs during replay; pin SDK
and model/provider code consistently with in-flight Workflow definitions.

Compared with the proposal's lazy descriptor, this uses the actual SDK model
without duplicating protocol behavior. It allocates and closes clients during
Workflow-side resolution, rather than prohibiting client construction entirely.
The protocol tests cover OpenAI Chat, OpenAI Responses, and Google, including
sandbox execution, Activity cleanup, and replay without repeating HTTP requests.

`docker compose up -d runner-worker` starts the Worker and pinned auto-setup server.
It creates `temporal` and `temporal_visibility` databases on the existing
PostgreSQL instance, including on an already initialized volume. Kapy's database
and schema migrations are separate. The host endpoint is
`127.0.0.1:${KAPY_TEMPORAL_PORT:-7233}`, and the Compose network endpoint is
`temporal:7233`.

```sh
docker compose build runtime
docker compose up -d temporal
docker compose run --rm --no-deps -e KAPY_TEMPORAL_ADDRESS=temporal:7233 \
  runtime python -m pytest -q -p no:cacheprovider tests/runner_duarable
```

Temporal owns model Activity timeout/retry behavior through SDK defaults. External
requests can repeat after an unrecorded completion; no exactly-once guarantee is
added. Workflow failures propagate to callers.

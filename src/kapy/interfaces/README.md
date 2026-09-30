# Independent interface processes

Each interface owns its configuration, event loop/server, background work and resource
cleanup. `kapy interface <name> ...` lazily calls one entry in the current process;
it does not launch or supervise child processes. Run HTTP and Telegram as separate
commands under the deployment's process manager. The entry contract is
`main(argv: list[str]) -> int`; built-in names and import paths live in
[cli/registry.py](../cli/registry.py).

```sh
kapy db upgrade
kapy interface http serve
kapy interface telegram db upgrade
kapy interface telegram serve
```

The equivalent direct entry is `python -m kapy.interfaces.<name>.main ...`.
Each process creates its own core PostgreSQL pool, Valkey client, SessionService
and Temporal Client through `open_resources()`. They communicate through core tables
and a common Valkey output namespace. HTTP also exposes these borrowed resources
on `app.state.resources`. SessionService resolves configuration per direct runner
start, and submits the existing RunnerWorkflow to the common Temporal task queue.
An independent `python -m kapy.runner_duarable.worker` owns Agent/model resources;
neither interface runs the legacy Agent or starts a Worker. Queue/cancel and
configuration APIs remain available, with their existing storage semantics.

Common environment values (read only during command execution):

| Variable | Default |
| --- | --- |
| KAPY_DATABASE_URL | postgresql://kapy:kapy-local@127.0.0.1:55432/kapy |
| KAPY_DATABASE_SCHEMA | kapy_tmpv2 |
| KAPY_VALKEY_URL | valkey://127.0.0.1:56379/0 |
| KAPY_VALKEY_NAMESPACE | kapy_tmpv2 (channel prefix adds :agent-output) |
| KAPY_TEMPORAL_ADDRESS | localhost:7233 |
| KAPY_TEMPORAL_NAMESPACE | default |
| KAPY_TEMPORAL_TASK_QUEUE | kapy-runner |
| KAPY_LOG_LEVEL | INFO |
| KAPY_HEARTBEAT_INTERVAL / KAPY_HEARTBEAT_TIMEOUT | 10 / 60 seconds |
| KAPY_TAKEOVER_GRACE_PERIOD | 30 seconds, after expired-token takeover |
| KAPY_REALTIME_OUTPUT | true |
| KAPY_OUTPUT_FLUSH_INTERVAL | 0.5 seconds |

Legacy lease users sharing core tables must use compatible heartbeat settings.
The durable Worker owns model/output policy and does not use those leases.
No command implicitly reads `.env`; export configuration or use `uv run --env-file`.
Private interface databases do not inherit the core URL or schema. The process manager's
own local database retains its existing independent lifecycle.

See [Telegram](telegram/README.md), [HTTP](http/README.md), and
[database ownership and migration](../database/README.md).

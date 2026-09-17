# Independent interface processes

Each plugin owns its configuration, event loop/server, background work and resource
cleanup. `kapy plugin <name> ...` lazily calls one entry in the current process;
it does not launch or supervise child processes. Run HTTP and Telegram as separate
commands under the deployment's process manager. The entry contract is
`main(argv: list[str]) -> int`; built-in names and import paths live in registry.py.

```sh
kapy db upgrade
kapy plugin http serve
kapy plugin telegram db upgrade
kapy plugin telegram serve
```

The equivalent direct entry is `python -m kapy.plugins.<name>.main ...`.
Each process creates its own core PostgreSQL pool, Valkey client, SessionService
and text Agent using ordinary application/ factories. They communicate through
core tables and a common Valkey output namespace, never shared Python objects.
Both use identical prompt/tool/dependency construction in application/agent.py;
this initial Agent has text output and no tools. SessionService resolves the
provider and model from the core catalog on each runner start.

Common environment values (read only during command execution):

| Variable | Default |
| --- | --- |
| KAPY_DATABASE_URL | postgresql://kapy:kapy-local@127.0.0.1:55432/kapy |
| KAPY_DATABASE_SCHEMA | kapy_tmpv2 |
| KAPY_VALKEY_URL | valkey://127.0.0.1:56379/0 |
| KAPY_VALKEY_NAMESPACE | kapy_tmpv2 (channel prefix adds :agent-output) |
| KAPY_LOG_LEVEL | INFO |
| KAPY_HEARTBEAT_INTERVAL / KAPY_HEARTBEAT_TIMEOUT | 10 / 60 seconds |
| KAPY_REALTIME_OUTPUT | true |
| KAPY_OUTPUT_FLUSH_INTERVAL | 0.5 seconds |

All workers sharing core tables must use compatible heartbeat and Agent settings.
No command implicitly reads `.env`; export configuration or use `uv run --env-file`.
Private plugin databases do not inherit the core URL or schema. The process manager's
own local database retains its existing independent lifecycle.

See [Telegram](telegram/README.md), [HTTP](http/README.md), and
[database ownership and migration](../database/README.md).

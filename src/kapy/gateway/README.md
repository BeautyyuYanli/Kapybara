# Gateway, CLI and Telegram

`create_app` composes the actual State, RPC, Execution, Skills and Agent exports.
Gateway owns its PostgreSQL metadata pool and HTTP client. Skills and agent payload
storage borrow the pool; State creates and closes its own pool and Valkey client.
No module reads `.env` implicitly. Settings retain `OPENAI_*` and `TELEGRAM_*` aliases;
other environment names start with `KAPY_`. Secrets are not stored in route configuration.

`kapy control-server` listens on `0.0.0.0:8000` by default (`--host` and `--port` override).
POST `/rpc` requires the configured administrator bearer and uses `dispatch_json`.
Machines connect to `/rpc/machines/{machine_id}` with an independent configured bearer
and `kapy.jsonrpc.v1`. Session capabilities bind the session and machine. Replacing a
connection fences the previous connection, and associations are ensured before calls.
`session.wait` delegates to State's non-consuming durable request receipt. Deterministic
mutation rejections have durable error receipts and never become deferred work. Session-create
intents save their initial Runner snapshot before calling State, so catalog changes cannot
block recovery of an already committed creation.

The local CLI uses Execution's authenticated Unix proxy. The inherited session identifies
the caller; `kapy control --session UUID ...` selects the target independently. An explicit
administrator bearer is used when no session context is present. A session capability always
keeps its session identity; an incomplete session context is rejected. Mutation request IDs
and skill archive paths are printed to stderr before transport begins; stdout contains results.
Prompt and SQL commands accept `--file PATH` or `--stdin`, and `session output` emits one JSON
record per line. Examples:

```sh
kapy control session create --machine docker-machine 'Inspect the project'
kapy control --session SESSION_UUID session input 'Continue'
kapy control --session SESSION_UUID session wait --request-id REQUEST_UUID
kapy control --session SESSION_UUID history export --output history.ndjson
kapy control --session SESSION_UUID --machine docker-machine skill upload ./example
kapy control --session SESSION_UUID --machine docker-machine skill download SKILL_UUID ./download
```

Skills preserve expected revisions and scoped idempotency. Archives move in 64 KiB chunks
through the existing machine file protocol, with at most two concurrent exchanges and a
16 MiB archive limit. Failed uploads retain their ZIP and request ID for explicit retry:
`skill upload --archive PATH --request-id UUID`. The CLI never substitutes a newly packed
archive under the previous request ID. Download extraction uses the Skills-owned validator.

Telegram saves complete incoming batches before advancing its polling offset. Each topic
has independent saved settings and active session. Resolved actions and UUID request IDs
survive restart. Output reads durable State records, saves pending projection and send
position, splits at 4000 UTF-16 units, and observes Telegram rate-limit delays. A send that
succeeds remotely before its receipt is saved can be repeated after a crash; delivery is
at least once. Only the configured chat is allowed, and existing session instructions
remain fixed when `/instructions` changes the settings for subsequent `/new` commands.

Gateway records machine provisioning before ensure so removing an association cannot lose
its cleanup obligation. Removing an association revokes access but does not release the
execution session: release is permanent on the daemon, so it is reserved for final deletion.
Deletion first records an outbox, asks State to stop its runner and delete the session,
then deletes Intelligence payloads and releases machine resources. Offline machines leave
recoverable cleanup obligations. Gateway authorization tombstones remain for request
receipts; they do not revive a deleted caller capability.

Module checks use isolated random PostgreSQL schemas and Valkey namespaces, mocked Bot API
and provider calls. Real execution-machine checks belong inside `kapy-v2-machine:dev`.
No cgroup prerequisite or host/Lody process investigation is part of the gateway.
`tests/gateway/test_machine_docker.py` runs only when `KAPY_DOCKER_TEST=1` inside a
container. Mount `src`, `tests`, and `pyproject.toml` read-only at `/workspace`, set
`PYTHONPATH=/workspace/src`, and run `/app/.venv/bin/pytest -p no:cacheprovider` in the
machine image. The container needs network access to the development PostgreSQL/Valkey
ports; process and filesystem isolation remain enabled. The regression starts the actual
`kapy server` entry, exercises authenticated child CLI calls and multichunk skill transfers,
checks reconnect, and waits for durable final deletion to remove the machine session.

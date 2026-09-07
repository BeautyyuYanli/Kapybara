# Gateway, CLI and Telegram

For a first start, inject environment variables explicitly: the CLI and Settings do not
automatically load `.env` or `.env.example`. Replace the placeholders below and set the
model's actual context window. The control server requires `OPENAI_API_KEY`,
`KAPY_CONTEXT_WINDOW_TOKENS`, `KAPY_CONTROL_TOKEN`, and `KAPY_SESSION_SIGNING_KEY`:

```sh
env OPENAI_API_KEY='<provider-api-key>' \
  OPENAI_BASE_URL='https://api.openai.com/v1' OPENAI_MODEL='gpt-5.6-luna' \
  KAPY_CONTEXT_WINDOW_TOKENS=100000 \
  KAPY_CONTROL_TOKEN='<control-admin-token>' \
  KAPY_SESSION_SIGNING_KEY='<random-signing-key>' \
  KAPY_MACHINE_TOKENS='{"docker-machine":"<machine-bearer>"}' \
  kapy control-server
```

PostgreSQL and Valkey must be reachable. Set `KAPY_DATABASE_URL` and `KAPY_VALKEY_URL`
for your network; development defaults use `127.0.0.1:55432` and `127.0.0.1:56379`.
`OPENAI_BASE_URL` and `OPENAI_MODEL` select the provider endpoint and model; other
settings use `KAPY_`. Telegram is optional: enabling `TELEGRAM_BOT_TOKEN` also requires
the allowed numeric `TELEGRAM_CHAT_ID`.

In the project machine container, start its daemon using the exact machine ID and bearer
from the control server's JSON mapping. This bearer is separate from the administrator
token. The loopback example assumes the control server is reachable in that network
namespace; use a reachable HTTPS control URL for a remote server:

```sh
env KAPY_CONTROL_URL='http://127.0.0.1:8000' \
  KAPY_MACHINE_ID='docker-machine' KAPY_MACHINE_TOKEN='<machine-bearer>' \
  kapy server
```

Run `kapy control` on that same machine, with access to the daemon's Unix socket
(`KAPY_DAEMON_SOCKET` overrides its XDG default). Daemon-managed session processes inherit
their session credentials. For an operator shell without session context, inject the
control administrator token explicitly:

```sh
env KAPY_CONTROL_TOKEN='<control-admin-token>' kapy control session list
```

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

`session update` replaces all settings and is allowed only while waiting. Its
`--help` describes how omitted configuration and default-machine options clear prior values.

Skills preserve expected revisions and scoped idempotency. Archives move in 64 KiB chunks
through the existing machine file protocol, with at most two concurrent exchanges and a
16 MiB archive limit. Failed uploads retain their ZIP and request ID for explicit retry:
`skill upload --archive PATH --request-id UUID`. The CLI never substitutes a newly packed
archive under the previous request ID. Download extraction uses the Skills-owned validator.

Telegram saves complete incoming batches before advancing its polling offset. Each topic
has independent saved settings and active session. Resolved actions and UUID request IDs
survive restart. Output reads durable State records, saves pending projection and send
position, splits rich replies conservatively and plain replies at 4000 UTF-16 units,
and observes Telegram rate-limit delays. A send that
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

Telegram replies use one stable nonzero `sendRichMessageDraft` ID per logical run in
private chats (including private topics), refreshing changed text and idle drafts
about every 20 seconds. Tool calls and waiting/attempt notices are not chat messages.
Completed model messages provide authoritative text; failed temporary attempts are
removed. Groups and group topics receive only the final reply. Final replies are
persisted with `sendRichMessage`, sending raw `rich_message.markdown`. Rich source
uses a conservative 32768 UTF-8 byte budget, splitting at blank lines outside ordinary
backtick/tilde fences. Oversized indivisible blocks fall back to plain `sendMessage`
within 4000 UTF-16 units, without truncation or synthetic wrappers. A draft is temporary, expires after roughly 30 seconds,
and is never a final receipt. Restart sends the active draft again. Explicit rich
content rejection falls back to plain draft; a plain draft HTTP 400 falls back to
final-only delivery. Errors remain natural, explicit failures without
raw exception strings. Normal replies and implicit session creation have no session
IDs; `/new` gives a brief confirmation.

Delivery projection version 1 reuses the existing cursor, pending text, and acknowledged
character offset. Terminal processing stops at the terminal record's cursor, leaving
later runs for another read. Same-route sessions follow their creating inbox update
order; pending or running replies block later sessions, while drained waiting sessions
do not. HTTP retry deadlines survive restart. A successful send with a lost response
or database acknowledgement may repeat the unacknowledged segment (at least once).

Upgrade contract: `empty_projection()` returns exactly `{"version": 1, "messages": {}}`.
A newly empty `{}` initializes normally. Any nonempty unversioned projection requires
operator migration and is left untouched; Gateway logs the required offline drain.
For the controlled upgrade, stop old control, verify every session has no active run,
every delivery cursor has reached output end, and no pending text or nonzero offset
exists. Only then replace projections with this empty shape while preserving cursors.
If input or output raced the stop, resume the old version and drain before trying again.
Never reset active delivery state, replay historical completed messages, or discard
unacknowledged text. No migration runner or legacy rendering emulation is provided.

Rich Markdown needs no MarkdownV2 escaping, parse_mode, or local renderer. Telegram
validates its structural limits (500 blocks, nesting depth 16, tables up to 20 columns);
its documented 32768 UTF-8 character limit is distinct from our conservative byte budget.
Commands and error pending bodies always use plain text. New final pending adds
`format: "rich"`; existing version 1 pending without that field remains plain.
`item_offset` counts acknowledged Python characters in original source, including CRLF,
never encoded bytes or added formatting. No migration is required.

Only the four verified rich HTTP/API 400 content-limit descriptions trigger persistent
`format: "plain"`; raw descriptions are neither saved nor logged. Unknown/non-content
400 stays blocked; 429, network errors, server failures, and lost acknowledgements
keep the original format and offset. A rich prefix may be followed by plain remaining
source if an oversized fence/table cannot be safely split. Draft format rejection or
an oversized indivisible preview sets a run-local plain marker and clears its send cache;
final rich formatting is attempted independently. Drafts may contain incomplete Markdown.
See https://core.telegram.org/bots/api#rich-message-formatting-options.

Content rejection evidence is recorded in `.context/delivery.md` at architect commit
`ec34cc0`: both rich methods returned HTTP 400, `ok: false`, `error_code: 400`, and
`Bad Request: ` followed by exactly one of `RICH_MESSAGE_TEXT_TOO_LONG`,
`RICH_MESSAGE_BLOCKS_TOO_MANY`, `RICH_MESSAGE_TABLE_COLS_TOO_MANY`, or
`RICH_MESSAGE_DEPTH_INVALID`. Classification requires all these conditions and exact
case-normalized code matching after the optional prefix. Guessed English parsing
messages and all other `RICH_MESSAGE_*` codes are deliberately unclassified.

# Telegram session plugin

Run one instance per bot and state file. The standalone process directly calls
SessionService for session business; it never loads the old gateway, ControlAPI,
principal/machine RPC or State output protocol.

```sh
# Core schema and an existing provider/model are prerequisites for serving.
kapy db upgrade
kapy plugin telegram db upgrade
kapy plugin telegram serve
```

`TELEGRAM_BOT_TOKEN` is required for serving. Set
`KAPY_TELEGRAM_ALLOWED_CHAT_IDS` to a JSON integer list, for example `[123456]`, and
`KAPY_TELEGRAM_SESSION_TEMPLATE` to an existing model identity, for example
`{"provider_id":"00000000-0000-0000-0000-000000000001","model_name":"your-model"}`.
The template accepts the existing CreateSession fields, including compaction values.
Provider credentials stay in the core catalog. Chat/model configuration cannot be
changed from Telegram. Optional `KAPY_TELEGRAM_API_BASE` defaults to
`https://api.telegram.org`, `KAPY_TELEGRAM_POLL_TIMEOUT` to 25 seconds and
`KAPY_TELEGRAM_RECOVERY_INTERVAL` to 5 seconds.

Private SQLite defaults to
`platformdirs.user_state_path("kapy") / "plugins/telegram/telegram.sqlite3"`, matching
the process manager's state root. On Linux this is
`$XDG_STATE_HOME/kapy/plugins/telegram/telegram.sqlite3`, or
`~/.local/state/kapy/plugins/telegram/telegram.sqlite3` when XDG_STATE_HOME is unset
or empty. Resolve the path at invocation, not import. Override with an absolute
`KAPY_TELEGRAM_DATABASE_PATH` or `--database-path` on `serve`/`db`; the command-line
value takes precedence. Mount this directory persistently, including WAL/SHM files.
Database commands need only this path, not core/Valkey/model/bot configuration.

The plugin owns `plugin_telegram_poll`, `plugin_telegram_inbox`,
`plugin_telegram_routes`, `plugin_telegram_delivery`, and the independent migration
version table `plugin_telegram_schema_version`. SQLite uses one queued connection,
WAL, FULL synchronization, a 5-second busy timeout and explicit transactions.
No runtime create_all, cross-database foreign keys, joins or transactions are used.

| Input | Behavior |
| --- | --- |
| /new [text] | Create/bind a session, optionally submit first queued input |
| Text or /queue text | Submit queued input; create a session if unbound |
| /steer text | Submit steer input; create a session if unbound |
| /status | Current UUID, active lease, cancel flag and channel queue sizes |
| /cancel | Request cancellation, without claiming the runner already stopped |
| /help | List these session commands |

Routes are keyed by bot/chat/topic (topic 0 when absent). Only allowed chats and
non-bot senders are handled. Media receives an unsupported notice; unknown commands
are never forwarded to the model. Status/cancel on an unbound route do not create
a session. A missing session clears its route; infrastructure errors do not.

Polling persists every batch and offset together before the next poll. Inbox
processing records the resolved target/template and creation/submission progress.
Core service commits and SQLite acknowledgements remain separate: a crash between
them may repeat creation or input. This is at-least-once business delivery, not an
idempotent request-ID protocol. Completed submission is saved before confirmation
messages, so a Telegram confirmation retry does not re-submit that input.

main.py owns runner tasks and absorbs SessionBusy; the controller merely requests
scheduling after submit_input. The input loop checks known Telegram sessions at
startup and periodically for queued/steer input without a valid lease, covering
crashes before scheduling. No global session scan or output polling is introduced.

Each durable delivery consumes SessionService.live, with its committed after_seq.
Private chats (including topics) receive replace/append draft previews sampled
from the latest state at most once per second, with unchanged drafts refreshed
after 20 seconds. Live consumption continues while a draft waits on chat pacing;
a committed message cancels and joins that provisional send before delivery. Groups only
receive complete text responses. Thinking/tools are provisional. Complete requests
are not echoed and complete messages are not interpreted as run-finished signals.
One pending response is persisted before sending, with rich/plain mode and confirmed
character offsets. Explicit rich-content rejection falls back to UTF-16-safe plain
chunks; other 400/403 errors block that delivery for operator investigation. Limit
and transport failures back off. Losing the remote send acknowledgement may repeat
a chunk. Pending sends finish before further live consumption or resuming history.

Live subscription timeouts reconnect from the persisted cursor. Temporary SQLite
discovery errors back off without cancelling existing session followers.

Switching /new preserves old deliveries. Ordering is per session; replies from
separate sessions in one topic can interleave. Reconnection drops provisional
preview state. Cancellation joins the outstanding live read before closing the
generator. SIGINT/SIGTERM stop all input, delivery and runner tasks before closing
Bot API, SQLite, Valkey and core PostgreSQL resources; exit does not set a user
cancel flag. Existing legacy Telegram state is not imported.

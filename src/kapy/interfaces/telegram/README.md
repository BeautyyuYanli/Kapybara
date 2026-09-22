# Telegram session interface

Run one instance per bot and state file. The standalone process directly calls
SessionService for session business; it never loads the old gateway, ControlAPI,
principal/machine RPC or State output protocol.

```sh
# Initialize both independent schemas before serving.
kapy db upgrade
kapy interface telegram db upgrade
kapy interface telegram serve
```

`TELEGRAM_BOT_TOKEN` is required for serving. Set
`KAPY_TELEGRAM_ALLOWED_CHAT_IDS` to a JSON integer list, for example `[123456]`.
Serving does not require a model: help/status work without a session template,
and requests that would create a session receive a configuration notice.
After configuring the provider/model through HTTP, send
`/model <provider UUID> <model name>` to select it for this bot. With a bound session,
the command also updates that session's provider/model pair. `/model` without
arguments shows the default and usage. The selection is saved in private SQLite
and takes effect immediately without restarting the interface process.

The optional `KAPY_TELEGRAM_SESSION_TEMPLATE` supplies the initial fallback, for example
`{"provider_id":"00000000-0000-0000-0000-000000000001","model_name":"your-model"}`.
The template accepts the existing CreateSession fields, including host paging values and context_plugin selection. Existing templates
without that field default to kapy/summary; the interface has no context logic.
An omitted template or JSON `null` requires selecting a model before creating a
session. A saved choice takes precedence over the entire environment template and
uses normal CreateSession defaults; model presets continue to come from the catalog.
Adding a model to the catalog does not automatically choose a Telegram default.
Provider credentials and catalog editing stay in the frontend; the Telegram command
only reads an existing model identity. Optional `KAPY_TELEGRAM_API_BASE` defaults to
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

The interface owns `plugin_telegram_poll`, `plugin_telegram_inbox`,
`plugin_telegram_routes`, `plugin_telegram_delivery`, `plugin_telegram_defaults`,
and the independent migration
version table `plugin_telegram_schema_version`. SQLite uses one queued connection,
WAL, FULL synchronization, a 5-second busy timeout and explicit transactions.
No runtime create_all, cross-database foreign keys, joins or transactions are used.
The `plugin_telegram_*` table names and `plugins/telegram` state path are stable
storage identifiers; moving the Python package to `interfaces` does not change them.

| Input | Behavior |
| --- | --- |
| /new [text] | Create/bind a session, optionally submit first queued input |
| Text or /queue text | Submit queued input; create a session if unbound |
| /steer text | Submit steer input; create a session if unbound |
| /status | Current UUID, active lease, cancel flag and channel queue sizes |
| /close | Close the session and registered plugin resources; repeat to retry failures |
| /cancel | Request cancellation, without claiming the runner already stopped |
| /model [provider UUID model name] | Show the bot default; with arguments, update the bound session and save the default |
| /help | List these session commands |

Routes are keyed by bot/chat/topic (topic 0 when absent). Only allowed chats and
non-bot senders are handled. Media receives an unsupported notice; unknown commands
are never forwarded to the model. Status/cancel/close on an unbound route do not create
a session. A missing session clears its route; infrastructure errors do not.

The default is shared by all allowed chats for that bot. A model command changes
only the current chat/topic's bound session and the bot default; other existing
sessions retain their configuration. Session updates preserve history, inputs,
model-setting overrides, title and compaction configuration. SDK-dependent
overrides are validated when execution starts; ordinary session updates keep them.
A running runner retains its model snapshot until its next start. The command
does not start or cancel execution, and an unbound chat does not create a session.

Session updates commit first, then the interface records that progress and saves the
default. These are separate database transactions. A transient SQLite failure
after the session update is retried before replying; there is no cross-database
rollback. Retries reuse the resolved session and model pair, and skip a session
update whose progress is already saved. Confirmation-send retries do not reapply
completed configuration. A crash before progress is saved can repeat the same
session update, following the inbox's existing at-least-once contract.

Polling persists every batch and offset together before the next poll. Inbox
processing records the resolved target/template and creation/submission progress.
Core service commits and SQLite acknowledgements remain separate: a crash between
them may repeat creation or input. This is at-least-once business delivery, not an
idempotent request-ID protocol. Completed submission is saved before confirmation
messages, so a Telegram confirmation retry does not re-submit that input.

main.py owns runner tasks and absorbs SessionBusy; the controller merely requests
scheduling after submit_input. The input loop checks ready Telegram sessions at
startup and periodically for queued/steer input without a valid lease, covering
crashes before scheduling. No global session scan or output polling is introduced.

Each durable delivery consumes SessionService.live batches in order, with its
committed after_seq. Pending sends finish before the next event in the batch;
only confirmed complete messages advance the persisted cursor.
Private chats (including topics) receive replace/append draft previews rendered once
per consumed batch. Delivery awaits that send, including retries and chat pacing,
before reading the next batch. SessionService/AgentOutput retain incoming output
and merge pending deltas during this wait; the interface has no read-ahead task,
coalescing timer or output queue. Preview state only retains the text and draft ID
needed for rendering. The Bot API client paces draft requests using
`KAPY_OUTPUT_FLUSH_INTERVAL` (default 0.5 seconds; zero uses 0.5 seconds for pacing);
complete messages retain one-second spacing. Unchanged drafts are suppressed for
20 seconds and may refresh on a subsequent batch; there is no idle refresh task.
Network latency and Telegram rate-limit backoff can slow delivery, including a
complete message arriving while a draft is still being sent. Groups only
receive complete text responses. Thinking/tools are provisional. Complete requests
are not echoed and complete messages are not interpreted as run-finished signals.
One pending response is persisted before sending, with rich/plain mode and confirmed
character offsets. Explicit rich-content rejection falls back to UTF-16-safe plain
chunks; other 400/403 errors block that delivery for operator investigation. Limit
and transport failures back off. Losing the remote send acknowledgement may repeat
a chunk. Pending sends finish before further live consumption or resuming history.

Live subscription timeouts and subscriber-buffer overflow reconnect from the
persisted cursor. Temporary SQLite
discovery errors back off without cancelling existing session followers.

Switching /new preserves old deliveries. Ordering is per session; replies from
separate sessions in one topic can interleave. Reconnection drops provisional
preview state. Cancellation unwinds the current read or send and closes the live generator. SIGINT/SIGTERM stop all input, delivery and runner tasks before closing
Bot API, SQLite, Valkey and core PostgreSQL resources; exit does not set a user
cancel flag. Existing legacy Telegram state is not imported.

`/close` first acquires the session lease, then awaits sequential plugin cleanup
and keeps the route/history for inspection. A busy lease replies that close has
not started and completes the inbox command; it is never automatically retried
as a future close. Use `/cancel` separately and retry `/close` after execution exits.
Failure preserves closing progress and replies with a retry notice. Input to a
closing/closed session is rejected without enqueueing; `/new` creates another one.
`/status` includes the lifecycle status. HTTP and Telegram share the application
registry/execution factory; neither controller allocates plugin resources.

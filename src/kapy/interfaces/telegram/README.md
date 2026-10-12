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
The template accepts CreateSession fields, including model settings, title and
fixed native `layers` configuration. For example,
add `"layers":{"shell":{"cwd":"/workspace","base_url":"http://shellctl:8765"}}`
to enable the shell Toolset in newly created durable sessions. State is initialized
and saved by SessionService/Worker.
An omitted template or JSON `null` requires selecting a model before creating a
session. A saved choice overrides only the template's provider/model pair, preserving
layers and other session options. Without a template it uses normal CreateSession
defaults; model presets continue to come from the catalog.
Adding a model to the catalog does not automatically choose a Telegram default.
Provider credentials and catalog editing stay in the frontend; the Telegram command
only reads an existing model identity. Optional `KAPY_TELEGRAM_API_BASE` defaults to
`https://api.telegram.org`, and `KAPY_TELEGRAM_POLL_TIMEOUT` to 25 seconds.
The durable Workflow owns execution recovery.

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
| /new [text] | Create/bind a session, optionally start a durable Workflow |
| Text | Start a durable Workflow; create a session if unbound |
| /status | Current UUID, lifecycle and active Workflow |
| /close | Close the session and registered layer resources; repeat to retry failures |
| /model [provider UUID model name] | Show the bot default; with arguments, update the bound session and save the default |
| /help | List these session commands |

Routes are keyed by bot/chat/topic (topic 0 when absent). Only allowed chats and
non-bot senders are handled. Media receives an unsupported notice; unknown commands
are never forwarded to the model. Status/close on an unbound route do not create
a session. A missing session clears its route; infrastructure errors do not.

The default is shared by all allowed chats for that bot. A model command changes
only the current chat/topic's bound session and the bot default; other existing
sessions retain their configuration. Session updates preserve history, model-setting overrides, title and layer configuration. SDK-dependent
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
Before core creation it saves a fixed UUID in resolved_action. For unfinished
creation actions, recovery queries that ID: ready binds the existing Session,
creating/closing/closed reports the phase and queryable/closable ID without
replaying enter, and only a missing record attempts creation with the same UUID.
Lease and registration reject concurrent duplicate
creation. The former route is preserved until ready is bound. A SessionCreationError
saves a terminal reply action; subsequent attempts only retry delivering that reply.
Its text retains the UUID for explicit queries or close requests. Core service
commits and SQLite acknowledgements remain separate; input can still repeat after
a crash between them. Completed submission is saved before confirmation messages,
so a Telegram confirmation retry does not re-submit that input.

Ordinary text and `/new <text>` directly await durable start confirmation, without
waiting for model completion. Busy submission replies explicitly and is not queued.
A non-successful terminal Workflow requires a new session; normal Worker recovery
continues the original Workflow. A Temporal RPC error may mean start was accepted;
the inbox records a status-query notice instead of automatically repeating that RPC.
A crash after acceptance but before saving inbox progress still follows the existing
at-least-once contract. Neither process shutdown nor request cancellation stops a
remote Workflow. No queue, steer or cancellation-flag commands are available.
Previously persisted obsolete commands return an unknown-command reply without
allocating a session.

Each delivery consumes SessionService.live batches in order with its persisted
`after_seq`. Every fully applied complete message advances this cursor. Complete ModelResponse text becomes a fixed formal
reply; complete messages without display text advance directly. Delta events
remain temporary previews. A complete message clears its own preview, and after
application all previews at/below the new cursor are discarded. Larger preview
positions remain, and non-text tool messages can display progress. Complete
requests are not echoed and complete messages do not signal run completion.

Private chats (including topics) receive replace/append draft previews rendered once
per consumed batch. Thinking previews show a sliding tail of the latest 500 Unicode
characters, without page boundaries or added separators. Replace/append events update
the response parts before the tail is selected; batch boundaries do not affect it.
Answer text takes precedence over thinking. Both use Markdown drafts, with plain-text
fallback on explicit format rejection.
Delivery awaits that send, including retries and chat pacing,
before reading the next batch. SessionService/AgentOutput retain incoming output
and merge pending deltas during this wait; the interface has no read-ahead task,
coalescing timer or output queue. Preview state only retains the text and draft ID
needed for rendering. The Bot API client paces draft requests using
`KAPY_OUTPUT_FLUSH_INTERVAL` (default 0.5 seconds; zero uses 0.5 seconds for pacing);
complete messages retain one-second spacing. Unchanged drafts are suppressed for
20 seconds and may refresh on a subsequent batch; there is no idle refresh task.
Network latency and Telegram rate-limit backoff can slow delivery, including a
complete message arriving while a draft is still being sent. Groups only
receive complete text responses; thinking and tool progress remain drafts.
One pending response is persisted before sending, with rich/plain format and its
acknowledged character offset. Successful chunks save progress, including across
restarts; completion clears pending and advances after_seq. There are no per-seq
reply receipts, edits or surplus-chunk deletions. Explicit rich-content rejection
falls back to UTF-16-safe plain chunks; other 400/403 errors block delivery for
operator investigation. Limit and transport failures back off. Losing a remote
send acknowledgment may repeat the unacknowledged chunk. Pending sends finish
before further live consumption or history replay.

Live subscription connection failures and timeouts reconnect from the persisted
cursor. A full subscriber buffer evicts the least recently updated events and keeps
the subscription open; SessionService.live recovers missed snapshots through history
reads. Temporary SQLite discovery errors back off without cancelling existing
session followers.

Switching /new preserves old deliveries. Ordering is per session; replies from
separate sessions in one topic can interleave. Reconnection drops provisional
preview state. Cancellation unwinds the current read or send and closes the live generator. SIGINT/SIGTERM stop all input and delivery tasks before closing
Bot API, SQLite, Valkey and core PostgreSQL resources. Existing legacy Telegram state is not imported.

`/close` first acquires the session lease, then awaits each configured Layer's
optional `exit_scope`. Normal close validates and saves
State after each hook attempt; the route/history remains available for inspection.
A busy lease or active durable Workflow replies that close has
not started and completes the inbox command; it is never automatically retried
as a future close. Retry `/close` after execution exits.
Cleanup failure preserves closing and any saved progress. The interface records a
retry reply for exhausted `RetryableScopeError`; uncaught
errors propagate without guaranteeing a reply. Creation failure replies with its
saved UUID for explicit query/close, hiding the original error; no fresh scope is
automatically created. Failed creating scopes exit all configured layers with None
in reverse order, including partial/uncalled enters. Exit must recover by ID/Config
and be idempotent; enter permits retry only by throwing RetryableScopeError.
Execution configuration updates to a creating/closing/closed session are
rejected; `/new` creates another one.
`/status` includes the lifecycle status. HTTP and Telegram share SessionService and the independent durable Worker. Durable runner is the only execution path.

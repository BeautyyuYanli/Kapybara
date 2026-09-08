# Product acceptance

This is the architect's acceptance inventory, not a claim that checks have passed.
Module tests are owned by seniors. End-to-end checks exercise the composed product
and installed CLI, using distinct PostgreSQL schemas and execution XDG directories.

| Area | Observable acceptance evidence |
| --- | --- |
| Packaging | uv sync --locked, Ruff, pyrefly, tests and uv build pass; installed kapy entry point starts both planes |
| Machine connection | Authenticated outbound WS connects; pending RPC fails promptly on disconnect; reconnect restores requests without losing still-running daemon processes |
| stdio | Exact stdout/stderr and exit status for ordinary and large output; no 8192-byte truncation; bounded resident memory through streaming/spooling |
| PTY | isatty true, interactive input and Ctrl-C, timeout leaves process alive, subsequent wait completes, concurrent PTYs independent |
| Process lifecycle | Process-group kill, child reaping and best-effort descendant cleanup in a dedicated Docker machine; session and machine context available in child CLI; escaped descendants are a documented limit |
| PTY buffer | At most 8192 raw bytes retained with explicit truncation/cursor information after sustained large output |
| Files | Direct chunk transfer and presigned HTTP upload/download preserve hash for a 64 MiB file; interrupted transfer is reported without claiming success |
| Sessions | Concurrent distinct sessions, no overlapping runs within one session, durable CRUD and default/multiple-machine selection |
| Steer/queue | Steer enters at a model/tool boundary; queue waits for waiting; unconsumed steer is retained; error/cancel paths do not strand queued work |
| Waiting events | One-shot results reach their sole receiver once, publication before waiting remains durable, duplicate publication and second receivers are rejected, and waiting does not settle input replies |
| Recursive control | Child CLI creates/inputs another session and gets session_id/waiting_id immediately; text completion or an in-loop ReplyTo hands its reply to the waiting parent once; partial replies return remaining addresses and continue the same loop, committed replies survive later failure/recovery; WaitFor settles no inputs |
| Persistence | After control-process restart, completed history replays and accepted pending work resumes under the documented interrupted-run semantics |
| Output | Delta order and cursor pagination preserve content, replay joins live consumption without omissions; reads never cross session boundaries |
| History | SQL, substring search and multilingual keyword examples work; malicious joins/subqueries/schema-qualified access/functions cannot read another session or mutate data |
| Agent | Live gpt-5.6-luna can invoke a real machine tool and finish; errors remain usable model/tool messages |
| Compression | Configurable threshold/window, newest content retained, historical levels advance and eventually drop; model request remains valid with tool-call/result pairing |
| Media | Supported media reaches provider; simulated provider rejection becomes text and subsequent loop remains valid |
| Plugins | Script tool uses declared parameter schema/description; apply-patch package applies a real workspace patch through the process manager |
| Skills | Safe archive CRUD, catalog substring filter, SKILL.md/full archive roundtrip, creation-time instruction catalog and later CLI refresh |
| Telegram | Fake Bot API verifies allowlisted chat/topic mapping, command settings/new session, message splitting/updates, restart offset and retry behavior |

## Load scenarios

- State: 100 fake-runner sessions, 20 submitted inputs each; count accepted, completed
  and replayed work; verify each session's ordering. Record wall time and throughput.
- Events: 100 independent one-shot channels and publish-before-wait cases; count distinct
  deliveries and observe completion latency without equating wakeup hints to delivery.
- Execution: 16 simultaneous interactive jobs, a 64 MiB output command and a 64 MiB
  file transfer; record buffer sizes, hashes, process cleanup and peak resident memory.
- Gateway: concurrent output polling during runs and a machine connection interruption;
  measure response latency and count explicit errors/timeouts, never silently discard.

Do not restart shared development database containers while senior tests are running.
Machine research and real process/file tests run in a dedicated Docker container,
not on the host or within Lody's runtime. Perfect cleanup of escaped descendants is
not a gate. Compression token counts must use provider API usage, per user direction.
Restart tests use an isolated control process/schema or dedicated disposable service.
Real Telegram messages are excluded until explicitly authorized. No throughput target
was requested, so report measurements rather than inventing a service-level guarantee.

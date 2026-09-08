# Partial reply integration and deployment

Merged main `fadef69` into `feat/steer-queue-message-waiting-id` at `d7446c5` using
`integrate/telegram-partial-replies`. The lead performed this integration directly.

Preserved the target branch's immediate atomic `RunContext.reply`, remaining-address
receipts, continuation in the same Pydantic AI run, batch/steer completion boundary,
recovery idempotency and ordered cycle outputs. Preserved main's provider/model catalog,
three protocol backends, per-run settings, native thinking events and concise Telegram
tool previews with per-message drafts and rich Markdown.

Resolved the conflict in `test_session_outputs.py`: retained ordered outputs with explicit
JSON shape assertions. The obsolete output-candidate recovery case is replaced by the
branch's commit/return/final interruption cases in `test_reply_progress.py`. Updated the
Responses compression fixture to v3/output arrays. Added the new `reply` history kind to
the agent's existing history schema instructions. No new public interface or dependency.

Validation used non-root Docker UID/GID 10001, dropped capabilities, no-new-privileges,
the project bridge and random database/Valkey scopes. Full suite: 450 passed and two
old-version fixture failures in 160.12s. After correcting only that fixture, its complete
three-protocol module passed all 23 tests in 2.70s. No production changes followed the
full run. Full Ruff check/format, src/tests/scripts Pyrefly (zero errors), and diff checks
pass. Existing google-genai deprecation warning remains.

The branch explicitly rejects pre-v3 Agent snapshots. Deployment will retain the old
`kapy_interaction_v2` schema unchanged and select `kapy_interaction_v3`, copying providers,
model IDs/defaults, skills, saved Telegram configuration and polling position. New Telegram
input starts a v3 session; old conversations are archived, not replayed or converted.
Deployment results and rollback details are recorded below after verification.

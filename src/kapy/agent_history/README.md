# Immutable agent history

`HistoryMessage(session_id, seq, message)` is one complete SDK message at a stored
session position. `HistoryRepository` borrows a transaction; it never commits,
claims operation ownership, or manages session lifetime. The physical table remains
`agent_history`, keyed by `(session_id, seq)` without physical foreign keys.

`append_history(id, messages)` requires the caller to serialize writers with its
conditional session-ownership write in the same transaction. Every entry uses one fixed pre-write maximum seq (-1 without history).
Only entries above that maximum are inserted, preserving input order. Stored
messages and numeric gaps at/below the maximum remain sealed; retries neither
replace them nor publish again. Invalid/non-integer/negative seq, duplicate positions
and cross-session entries reject the whole batch before writes. Metadata is payload;
columns alone determine storage position.

SDK parts and message metadata use the official ModelMessagesTypeAdapter codec.
Response finish_reason and usable positive-input/nonnegative-output token counts
are normalized in columns; remaining SDK response fields are retained. Reads do
not reconstruct every provider-specific usage detail. Appends return accepted input
DTOs, so post-commit broadcasts retain their full original SDK usage.

`read_history_entries(id, after_seq=-1)` returns ascending complete entries after
an exclusive integer cursor >= -1. `read_history_page(id, before_seq, limit)` returns
the latest matching window in ascending order; has_more means older rows exist.
Missing session history returns empty values. Cursor validation and pagination are
independent of durable snapshot encoding.

[`kapy.runner.recording.Recorder.persist_messages`](../runner/recording.py) borrows
Worker resources from run deps, confirms current ID/phase/deadline using conditional
writes in a short transaction, then
publishes accepted messages through [agent_output](../agent_output/README.md) after
commit. Recording and final runner-state saving remain separate transactions: a
failure before the final save commits can leave recorded history while the
previous successful snapshot remains. SessionService's
[Temporal admission](../control/README.md) prevents starting a new run after such
failure. `live()` combines these immutable messages with best-effort previews.

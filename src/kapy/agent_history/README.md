# Immutable agent history

`HistoryMessage(session_id, seq, message)` is one complete SDK message at a stored
session position. `HistoryRepository` borrows a transaction; it never commits,
claims session ownership, or manages session lifetime. The physical table remains
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
Worker resources from run deps, confirms session ID/native UUID/run_id using conditional
writes in a short transaction, then
publishes accepted messages through [agent_output](../agent_output/README.md) after
commit. The same transaction saves the runner checkpoint and every layer State.
The first model request, successful model nodes, complete tool batches and final
after_run/resource teardown each form a checkpoint, including empty message tails.
The runner validates its confirmed boundary and rejects occupied old positions
before append_history; an incomplete accepted batch rolls back the whole save.
Acknowledged seq is applied before best-effort broadcast. Uncertain commits stop
execution without retries; explicit resume validates the actual stored checkpoint
and every history position/content projection. `live()` combines these immutable
messages with best-effort previews. See [runner](../runner/README.md).

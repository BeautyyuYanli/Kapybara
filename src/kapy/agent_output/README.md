# Agent output transport

`AgentOutputService` borrows an asynchronous Valkey client. Application/Worker code
owns that client's lifetime and uses an environment-specific channel prefix.
Pub/Sub is best-effort, with no offline storage, TTL, persistence or outbox.
`TextDelta`, `MessageCommitted`, `OutputEvent` and OutputCallback live in this module;
complete-message DTOs and persistence live in [agent_history](../agent_history/README.md).

TextDelta replace initializes or clears one text/thinking part; append preserves
whitespace. Tool arguments/results are represented by complete messages. Deltas
carry a predicted response_seq but never advance a replay cursor. Different
execution attempts may share that prediction. MessageCommitted means one immutable
complete message, without implying execution completion.

Each `publisher(id, flush_interval=0.5)` owns one bounded buffer and background
send/recovery task. Intervals are finite and nonnegative; zero wakes immediately.
Buffers and each in-flight batch are at most 64 KiB. Complete snapshots replace their
buffered key; pending same-part previews merge and retain the latest arrival order.
No merge crosses a same-key snapshot. Network work never blocks the producer callback;
ordinary transport failures disable/drop output. Recovery drops new events and
probes independently. Normal exit drains within two seconds; exceptional exit
discards buffered events and propagates the body's failure/cancellation.

Each `subscribe(id)` confirms subscription before yielding and owns a dedicated
Pub/Sub connection and background receiver. Consumer backpressure does not stop
network reception. Pending events merge with the same rules and are bounded to
256 entries; oldest entries are evicted regardless of message type. Errors precede
buffered data on the next read; idle reads have no timeout/retry. Exit cancels and
joins the receiver even if iteration never begins. Valkey database numbers do not
isolate channels.

Direct subscribers own their replay policy. `SessionService.live()` subscribes
before reading history, deduplicates/sorts complete messages by seq, backfills numeric
jumps and polls for missed commits. Consumers acknowledge only fully applied complete
messages, clear previews on reconnect, and remove previews at/below each applied
seq. Durable history recording commits before broadcasting. A crash between these
steps can miss a broadcast; history polling or reconnection recovers it.

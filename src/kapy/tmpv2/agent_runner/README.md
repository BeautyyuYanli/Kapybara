# Agent runner

`open_runner` acquires one session's logical execution lease and yields a handle.
It loads only the checkpoint, next absolute message sequence and latest summary.
Use and close it in the task that opened it: `Agent.iter()` owns task-local AnyIO
cancel scopes. All handle operations reject concurrent, reentrant or cross-task calls.
The separate heartbeat task only uses its own short database transactions.

The application owns the configured Agent, deps, PostgreSQL engine and async
session factory. It creates tables from `agent_metadata` and, for session inputs,
`ControlTable.metadata`; the services never create tables or close shared clients.
The repository currently relies on PostgreSQL READ COMMITTED row locks, conditional
upsert and `clock_timestamp()`. Table declarations use the connection's default
schema and deliberately contain no physical foreign keys.

The runner can execute independently of business session configuration. For a
fresh database, this example uses the local SDK test model; pass an
application-owned `postgresql+psycopg://...` database URL:

```python
from uuid import uuid4

from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kapy.tmpv2.agent_runner import open_runner
from kapy.tmpv2.agent_runner.models import agent_metadata


async def example(database_url: str):
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(agent_metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with open_runner(
            uuid4(), agent=Agent("test"), session_factory=session_factory
        ) as runner:
            await runner.rebuild_context()
            result = await runner.turn(steer=["Hello"])
            while not result.finished:
                result = await runner.turn()
            return result
    finally:
        await engine.dispose()
```

[SessionService](../control/README.md) is the higher-level user entry point. It
stores session configuration, resolves the model, then composes the input loop.
Steer is accepted at request boundaries; queued input starts another run after a
normal return; cancel ends the current run without modifying its checkpoint. A
cancellation signal is the presence of a `session_cancels` row. Each service operation
owns its required checks; see the [control service boundaries](../control/README.md).
The runner itself knows only an identifier, model-independent input callbacks and
heartbeat values; it does not import the service or read its configuration tables.
Input consumption borrows the fenced transaction that appends the accepted request
to history; there is no separate acknowledgement.

InputBatch snapshots contain candidates, not a guarantee that every input is still
pending. Its consume callback returns only the actual contents deleted by the
checkpoint transaction, preserving snapshot order. Withdrawn inputs are excluded
from both history and the SDK request. A new run prepares dynamic prompts outside
the transaction; if consumption finds fewer candidates, it rolls back and rebuilds
the SDK graph from that smaller set before trying again. No model or tool is retried.
An entirely withdrawn batch creates no history and starts no model request; an
already-pending checkpoint continues normally. Callbacks must remain limited to
the original immutable input IDs, so each preparation retry strictly shrinks.

History contains complete requests and responses, plus their message metadata and
finish reason. Normalized input/output token counts are stored in separate nullable
columns; other usage fields are discarded. A committed tool response can be recovered
without repeating its model request; a committed tool-result request can be
recovered without repeating tools. Model/tool work between checkpoints may replay
and external side effects are not exactly once. Deferred tools, provider-suspended
responses, and hooks that rewrite existing history are outside this module's
execution model. A stale runner may finish external work but cannot commit it.

For manual execution, call `await runner.rebuild_context()` before `turn()` or
`compact()`. `run()` prepares context automatically. Preparation loads only the
latest summary's replay window and subsequent history; without a summary it loads
all history once. The handle retains a committed working context and a separate
mutable SDK graph, not another permanent copy of full history. Normal turns append
only their newly committed messages. Absolute database sequences never derive from
context length or SDK message indices.

`await runner.compact(max_retries=2)` temporarily asks the same configured Agent
for a text summary and saves it in `agent_compactions`, anchored to the latest
committed message. It leaves the current context, graph and checkpoint unchanged;
call `rebuild_context()` to apply the summary. Empty history returns `None`, and a
repeated call at the same anchor returns the saved summary. `handle_response` must
finish its pending tool/output batch before compaction is allowed.

Only input and model nodes execute during this temporary call. Non-text responses
receive bounded format retries with paired tool replies; client tools and business
output validators never execute. Original tool definitions, native server tools,
output schema, settings, initialization and request hooks remain active. A config
that forces non-text output may exhaust retries and fail. Temporary messages and
usage never enter business history or checkpoints. Failed execution invalidates the
handle; reopening recovers its committed checkpoint and any saved summary.

Keep Agent-level `max_concurrency=None` (the SDK default). Its built-in limiter
holds a token for the entire `Agent.iter()` lifetime and rejects the same task's
nested compaction run, even with a limit greater than one. This lifecycle therefore
does not support Agent-level concurrency limits. When calling the lower-level
runner directly, request limits can use `Agent(ConcurrencyLimitedModel(model, limiter=N))`
from `pydantic_ai.models.concurrency`; its token covers each model request, so the
paused main graph does not hold it. `SessionService.start_runner` overrides the
Agent's original model with the session-configured model, so a concurrency wrapper
on that original model does not apply. Worker limits can wrap the complete
`start_runner()` call for either entry point.

`run()` and the runner module's `start_runner()` accept
`compaction_threshold_tokens=None` and `compaction_replay_turns=10`.
SessionService instead reads these values from the session: a stored None threshold
resolves to 70% of model capacity, or rejects startup if capacity is unknown. A positive
threshold enables automatic summaries at safe boundaries, including done. The
latest business response's input+output count must exceed it and lie after the
latest summary anchor. Unknown usage suppresses triggering, cache counts are not
added again, and summary usage does not affect this observation. This is an observed
size, not a pre-request context limit: pending inputs and tool results are not counted.
Cancel is checked before preparation/compaction, and summary output never replaces
the business turn result.

Replay N is nonnegative; zero omits replay. Otherwise choose the Nth response
backwards from the summary anchor, include its preceding consecutive requests,
then extend backwards to include every crossing tool call/reply pair (including
tool retries and parallel batches). Fewer than N responses includes the whole
prefix. Paging cannot change the selected window. Context contains the original
system parts once, a virtual summary prompt, this fixed replay, a virtual resume
prompt, then all subsequent history. These virtual messages are never persisted or
executed as inputs; done with no real input stays done. Each run applies its own N
once and again after a new summary; ordinary turns never re-read full history.

Optional live output is composed by `SessionService`, using an application-owned
`valkey.asyncio.Valkey` client:

```python
from contextlib import aclosing

from kapy.tmpv2.agent_output import AgentOutputService
from kapy.tmpv2.control.sessions import MessageCommitted, SessionService

sessions = SessionService(
    session_factory,
    output_service=AgentOutputService(valkey_client, channel_prefix="my-environment:agent-output"),
)

# Producer: the service owns the publisher across all queued runs.
await sessions.start_runner(session_id, agent=agent, realtime_output=True)

# Observer: run independently; this stream continues across runner lifetimes.
async with aclosing(sessions.live(session_id, after_seq=-1)) as events:
    async for event in events:
        if isinstance(event, MessageCommitted):
            ...  # Save/replace the complete message at event.message.seq.
        else:
            ...  # Apply the provisional text part's replace/append operation.
```

`realtime_output=False` (default) needs no output service, starts no publisher and
does not request SDK streaming. Direct users can pass `on_output` to `run()` or
the runner module's `start_runner()`. The callback applies only to that run call;
`turn()` does not inherit it. It is awaited in the runner's task, outside database
transactions. Callback errors invalidate the handle. Only business model text and
readable thinking stream; compaction summaries and tool argument deltas do not.
The SDK capability dynamically enables streaming on each model node, preserving
ordinary node hooks and a native graph retained across successive run calls.

`TextDelta` identifies `(session_id, response_seq, part_index)` with `part_kind`
`text` or `thinking`. `replace` initializes/clears a part, `append` adds text without
trimming. `response_seq` is the next absolute history sequence captured before the
model request. `MessageCommitted.message` is a `HistoryMessage(session_id, seq,
message)`, using the same normalized usage and official SDK codec as history reads.
Both input and response/tool-result checkpoints publish after commit, reusing the
INSERT payload with no extra SELECT or RETURNING. State-only checkpoints emit
nothing. A committed message replaces all temporary parts at its sequence; it
is not a runner-completion event. Output DTOs carry no execution lease token.

`live(after_seq=-1)` starts after the last applied complete history sequence (-1 replays all),
confirms its subscription before reading history, filters overlaps and backfills
missing predecessors when later events expose a gap. It also polls history every
5 seconds, configurable through `SessionService(live_poll_interval=...)`, so lost
final notifications are recovered without another event or reconnect. Only the
contiguous prefix advances the cursor; unresolved gaps wait for another read.
Each short transaction ends before yielding. Consumer backpressure pauses polling;
incoming traffic does not postpone it. Each live call owns at most one pending
subscription read and joins it before closing its subscription. Database and
subscription errors end the generator; runner completion does not. Reconnect from
the last complete seq, clearing provisional text first. Missing subscribers lose
deltas, and temporary append events may occasionally repeat or interleave between
runner attempts; committed messages replace all provisional content. There is no
outbox or delta recovery cursor.

`AgentOutputService.publisher(flush_interval=0.5)` buffers at most 64 KiB of encoded
JSON plus one in-flight batch of the same maximum size. The awaited callback only
encodes and buffers locally: it never waits on network I/O and drops ordinary errors,
overflow, oversized events, and events during recovery or after close. First arrival
starts the batching deadline; commits, capacity and zero interval wake the background
task immediately. `SessionService.start_runner(output_flush_interval=...)` forwards
this interval. One task sends and recovers connections, including with zero interval.
Each network attempt has a one-second deadline. Failed batches are discarded;
recoverable failures trigger a one-second delay and a bounded PING probe until
recovery, independently of new events. Unrecoverable errors disable that publisher.
Every context exit drops pending output and cancels/joins the task without a final
flush, so the final commit notification may be recovered through database polling.
`subscribe()` yields individual events after acknowledgement, allows backpressure,
and propagates connection/decode errors without automatic resubscription. Idle reads
have no timeout. Shared clients stay open.

The channel is `{channel_prefix}:{session_id}` and carries nonempty JSON arrays of
these two events. Prefixes must isolate environments because Pub/Sub ignores the
Valkey database number. This uses ordinary PUBLISH/SUBSCRIBE, including with a
direct-node async client in Cluster, not sharded Pub/Sub. No database schema changes,
Valkey keys, TTLs or output heartbeat are required; the execution lease heartbeat
retains its existing purpose.

The relevant tests are `tests/tmpv2/agent_runner`. The SDK contract tests require no
services. Integration tests use real PostgreSQL at `KAPY_DATABASE_URL` (defaulting
to the repository's local port 55432), own random schemas, and exercise actual
connections/processes. Output integration tests also require Valkey at
`KAPY_VALKEY_URL` (default local port 56379), use unique session channels and
close their clients. Run all of them with `uv run pytest tests/tmpv2/agent_runner`.

# Agent runner

`open_runner` borrows ownership from the independent `kapy.session_lease` component
and yields a runner handle. `session_leases` contains ownership only; acquiring a
lease creates neither business sessions nor runner checkpoints.
It loads only the checkpoint, next absolute message sequence and latest context page.
Use and close it in the task that opened it: `Agent.iter()` owns task-local AnyIO
cancel scopes. All handle operations reject concurrent, reentrant or cross-task calls.
The lease maintains one heartbeat task using short transactions, through native
graph cleanup. Checkpoint/history and queue consumption first call
`lease.lock_owned(db)` in the same transaction; all cooperating writers take the
lease row before business rows. Bypassing that protocol is not automatically fenced.

`open_runner` and `start_runner` accept either direct `agent`/`deps` or an
`execution_factory`, exclusively. The factory is an async context manager yielding
`RunnerExecution(agent, deps, context_policy, capabilities)` inside the acquired
lease. It receives no lease handle. SDK graph cleanup precedes factory exit, which
precedes lease release. It is recreated on reacquisition; the core still needs no
business session record and does not import plugin implementations. Application
factories compose [Agent plugins](../agent_plugins/README.md) with a fresh Agent.

The application owns the configured Agent, deps, PostgreSQL engine and async
session factory. `kapy db upgrade` migrates `agent_metadata`, `lease_metadata` and the control tables;
the services never create tables or close shared clients. Isolated tests can
initialize disposable schemas directly from their metadata.
The lease relies on PostgreSQL READ COMMITTED row locks, conditional upsert and
`clock_timestamp()`. It can also be used by non-runner operations through
`open_session_lease`; `SessionBusy` and `RunnerLost` remain compatible exports
(the latter aliases `LeaseLost`). A timeout permits takeover; it does not revoke
an unchanged token or undo external requests. Table declarations use the connection's default
schema and deliberately contain no physical foreign keys.

The runner can execute independently of business session configuration. For a
fresh database, this example uses the local SDK test model; pass an
application-owned `postgresql+psycopg://...` database URL:

```python
from uuid import uuid4

from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kapy.agent_runner import open_runner
from kapy.agent_runner.models import agent_metadata
from kapy.session_lease.models import lease_metadata


async def example(database_url: str):
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(agent_metadata.create_all)
            await connection.run_sync(lease_metadata.create_all)
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
`turn_context_page()`. `run()` prepares context automatically. `turn()` advances
one complete model/tool batch without automatic paging; `run()` checks the injected
policy at its existing safe boundaries, including done. Cancel precedes preparation,
paging and input acceptance. A page action never replaces the business turn result.

`SessionExecutionCapability` owns SDK initialization recovery, input acceptance and
node checkpoints through Pydantic AI 2.40.0's public hooks. It is injected into each
business `Agent.iter()` alongside `OutputCapability`, without changing shared Agent
configuration or caller deps. Its outermost node ordering checkpoints after ordinary
business after-node hooks. Protocol/transaction failures are latched independently
of SDK error recovery. The thin runner owns the lease scope, task constraint,
graph lifetime and queued/cancel scheduling; SessionLease maintains the heartbeat.
The runner never writes SDK private history.

The handle keeps a committed working context and a separate SDK graph. Absolute
history sequences never derive from SDK message indices or virtual context length.
A new graph receives the assembled view as `message_history`; an existing graph
applies rebuilt context through `before_model_request`, preserving the SDK's freshly
resolved pending content, instructions and metadata. Ordinary turns append their
committed messages without rereading all history.

`open_runner(..., context_policy=...)` and the lower-level `start_runner` accept one
`ContextPolicy` for their entire lifetime. Omission selects `full_history_policy()`:
no automatic trigger/action, with all original history retained. `run` and
`rebuild_context` have no summary-specific parameters. A policy combines independent
callbacks rather than requiring a subclass:

```python
from kapy.agent_runner import open_runner, summary_context_policy

policy = summary_context_policy(
    agent, deps=deps, threshold_tokens=100_000, replay_turns=10, max_retries=2
)
async with open_runner(
    session_id, agent=agent, deps=deps, session_factory=factory, context_policy=policy
) as runner:
    await runner.rebuild_context()
    page = await runner.turn_context_page()
```

`ContextPolicy(key, should_turn, on_turn, assemble)` separates trigger, action and
assembly. The pure trigger receives `PageBoundary` with checkpoint, current/previous
anchors and normalized latest-response usage. The optional async action receives
`PageTurnContext`: session, fixed anchor, prior page, a copied committed view, bounded
history readers and stable `operation_id` (`session:policy:anchor`). It returns a JSON
object; absent action stores `{}`. Actions run outside transactions while the lease
heartbeat continues. External effects may repeat before page commit: use that
operation ID for idempotency or reconciliation when needed.

The async assembler receives `ContextAssemblyContext` with page payload (or None),
`prefix_through_seq`, and readers bound to that prefix. `read_history` returns ascending
inclusive ranges; `read_history_before` returns descending bounded pages. Each call
uses its own short transaction. The assembler returns only a replacement prefix.
The core appends original post-anchor history and the pending checkpoint suffix,
extending backwards to close tool call/result pairs. Even replay zero cannot remove
a pending request or its required tool results. Strategies must keep their returned
prefix internally paired and must not rerun actions, consume inputs or mutate history.

`turn_context_page()` requires prepared context and a model_request/done boundary;
handle_response must finish its saved tool/output batch first. Empty history returns
None. It fences the lease, runs the action, validates JSON, fences and commits an
immutable `agent_context_pages` row, then assembles/applies the new view. Repeated
calls at one anchor reuse its saved payload. Page state never advances checkpoint
or absolute history seq. Assembly failure after commit invalidates the handle;
reopening reassembles the saved page without repeating its action. Stored policy_key
must match the injected policy; unknown protocols fail instead of losing context.

`summary_context_policy` implements `summary/v1`: optional token trigger, a temporary
text summary action, and summary/replay/resume assembly. Positive threshold enables
automatic summaries when the latest business response's input+output tokens exceed
it and its seq is later than the previous page anchor. Unknown usage does not trigger;
cache usage is not counted again and summary usage never changes this observation.
This is an observed size, not a pre-request limit. `threshold_tokens=None` disables
automatic paging but still permits manual summaries.

The summary action runs only input/model nodes of the same configured Agent. Non-text
responses receive bounded paired format retries; client tools and business output
validators never execute. Tool definitions, native server tools, settings and request
hooks remain active. Auxiliary graphs do not receive SessionExecutionCapability or
business OutputCapability. Keep Agent-level `max_concurrency=None`: its run-wide
limiter rejects nested page-action graphs. Request limits may use
`ConcurrencyLimitedModel`; SessionService overrides the original Agent model, so
limits on that original model do not apply to service-created models.

The default assembler retains original system parts once, summary, N replay responses
with backwards tool-pair closure, and a resume prompt; the core appends the protected
raw suffix. Replay reads are restricted to the replaceable prefix. N is nonnegative;
zero omits replay, never required continuation. Virtual messages are not persisted
or accepted as user inputs. Done without new real input remains done.

SessionService constructs a policy for each acquired execution outside its configuration
transaction, using the actual Agent. Configuration stays fixed across queued/reacquired
runners; each factory owns its policy and plugin contexts. Default summary settings come from the
session's compaction_threshold_tokens and compaction_replay_turns. A stored None
threshold resolves to 70% of model capacity or rejects startup if capacity is unknown.
Creation with unknown capacity and no threshold stores 183500; updates do not default
it. An injected `context_policy_factory(session, model_record, agent)` may use another
strategy without interpreting those summary fields. HTTP and Telegram share the
application's service factory and never choose a context strategy in controllers.


Optional live output is composed by `SessionService`, using an application-owned
`valkey.asyncio.Valkey` client:

```python
from contextlib import aclosing

from kapy.agent_output import AgentOutputService
from kapy.control.sessions import MessageCommitted, SessionService

sessions = SessionService(
    session_factory,
    output_service=AgentOutputService(valkey_client, channel_prefix="my-environment:agent-output"),
)

# Producer: the service owns the publisher across all queued runs.
await sessions.start_runner(session_id, agent=agent, realtime_output=True)

# Observer: run independently; this stream continues across runner lifetimes.
async with aclosing(sessions.live(session_id, after_seq=-1)) as batches:
    async for batch in batches:
        ...  # Send the whole list as one JSON array frame.
        for event in batch:
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
readable thinking stream; summary page actions and tool argument deltas do not.
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
History and each subscription read produce nonempty lists, not individual events.
The WebSocket controller sends one JSON array per batch. Batch boundaries are not
transaction boundaries or acknowledgements. Each short transaction ends before
yielding. Consumer backpressure pauses polling, but not background reception;
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
this interval. Before encoding each batch, the sender merges pending deltas by
(response_seq, part_index, part_kind): append joins text, replace discards earlier
text (including an empty replacement), and commits remove deltas through their seq.
Only this unsent batch participates; already delivered text is not retained.
One task sends and recovers connections, including with zero interval.
Each network attempt has a one-second deadline. Failed batches are discarded;
recoverable failures trigger a one-second delay and a bounded PING probe until
recovery, independently of new events. Unrecoverable errors disable that publisher.
Every context exit drops pending output and cancels/joins the task without a final
flush, so the final commit notification may be recovered through database polling.
`subscribe()` starts one receiver that owns connection setup, acknowledgement and
cleanup. The outer context waits for readiness and always cancels/joins the receiver,
even before the first iterator read. The receiver keeps merging incoming events
into one flat list while the consumer is busy. Each read takes the whole list and
replaces it with an empty one, without waiting to fill a batch. Previously delivered
lists are never mutated. No busy polling or additional database task is involved.

The subscriber uses the same merge rules across all undelivered network batches;
its maximum observed commit seq also rejects late covered deltas. Pending JSON is
limited to 1 MiB: overflowing delta updates leave the old buffer intact; commits
first evict deltas, then raise BufferError if complete messages alone cannot fit.
The limit excludes decoded network input and batches already handed to consumers.
Connection/decode/capacity errors release the connection immediately and take priority
over buffered output on the next read; there is no automatic resubscription. Idle
reads have no timeout. Exiting the context joins the receiver before closing the
iterator. Shared clients stay open.

The channel is `{channel_prefix}:{session_id}` and carries nonempty JSON arrays of
these two events. Prefixes must isolate environments because Pub/Sub ignores the
Valkey database number. This uses ordinary PUBLISH/SUBSCRIBE, including with a
direct-node async client in Cluster, not sharded Pub/Sub. No database schema changes,
Valkey keys, TTLs or output heartbeat are required; the execution lease heartbeat
retains its existing purpose.

The relevant tests are `tests/agent_runner`. The SDK contract tests require no
services. Integration tests use real PostgreSQL at `KAPY_DATABASE_URL` (defaulting
to the repository's local port 55432), own random schemas, and exercise actual
connections/processes. Output integration tests also require Valkey at
`KAPY_VALKEY_URL` (default local port 56379), use unique session channels and
close their clients. Run all of them with `uv run pytest tests/agent_runner`.

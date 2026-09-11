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

Import `SessionService` before creating the control tables: its repository import
registers the input and cancel models. Importing only `ControlTable` does not
register them. For a fresh database, this complete example uses the local SDK test
model; pass a `postgresql+psycopg://...` URL for an application-owned database:

```python
from uuid import uuid4

from pydantic_ai import Agent
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from kapy.tmpv2.agent_runner.models import agent_metadata
from kapy.tmpv2.control.database import ControlTable
from kapy.tmpv2.control.sessions import SessionService


async def example(database_url: str):
    engine = create_async_engine(database_url)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(agent_metadata.create_all)
            await connection.run_sync(ControlTable.metadata.create_all)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        sessions = SessionService(session_factory)
        session_id = uuid4()
        await sessions.enqueue_input(session_id, "queued", "Hello")
        return await sessions.start_runner(session_id, agent=Agent("test"))
    finally:
        await engine.dispose()
```

`SessionService.start_runner` composes the complete input loop. Steer is accepted
at request boundaries; queued input starts another run after a normal return;
cancel ends the current run without modifying its checkpoint. A cancellation
signal is the presence of a `session_cancels` row. Session identifiers require no
business session row. Input consumption borrows the same fenced transaction that
appends the accepted request to history; there is no separate acknowledgement.

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
does not support Agent-level concurrency limits. For request limits, configure
`Agent(ConcurrencyLimitedModel(model, limiter=N))` using
`pydantic_ai.models.concurrency.ConcurrencyLimitedModel`; its token covers each
model request, so the paused main graph does not hold it. Worker limits can instead
wrap the complete `start_runner()` call. The runner never changes these settings.

`run()` and both `start_runner()` entry points accept
`compaction_threshold_tokens=None` and `compaction_replay_turns=10`. A positive
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

The relevant tests are `tests/tmpv2/agent_runner`. The SDK contract tests require no
services. Integration tests use real PostgreSQL at `KAPY_DATABASE_URL` (defaulting
to the repository's local port 55432), own random schemas, and exercise actual
connections/processes. Run all of them with `uv run pytest tests/tmpv2/agent_runner`.

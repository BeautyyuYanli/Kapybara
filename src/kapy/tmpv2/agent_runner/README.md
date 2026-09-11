# Agent runner

`open_runner` acquires one session's logical execution lease and yields a handle.
Use and close it in the task that opened it: `Agent.iter()` owns task-local AnyIO
cancel scopes. `turn` and `run` reject concurrent, reentrant or cross-task calls.
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
finish reason. Usage is not persisted. A committed tool response can be recovered
without repeating its model request; a committed tool-result request can be
recovered without repeating tools. Model/tool work between checkpoints may replay
and external side effects are not exactly once. Deferred tools, provider-suspended
responses, and hooks that rewrite existing history are outside this module's
execution model. A stale runner may finish external work but cannot commit it.

The relevant tests are `tests/tmpv2/agent_runner`. The SDK contract tests require no
services. Integration tests use real PostgreSQL at `KAPY_DATABASE_URL` (defaulting
to the repository's local port 55432), own random schemas, and exercise actual
connections/processes. Run all of them with `uv run pytest tests/tmpv2/agent_runner`.

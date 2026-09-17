import asyncio
from contextlib import asynccontextmanager
from typing import Any, cast
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from kapy.agent import AgentPayloadStore


@pytest.mark.asyncio
async def test_repeated_cancellation_waits_for_payload_write_before_cleanup() -> None:
    started, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class Connection:
        async def execute(self, query: Any, params: Any) -> None:
            started.set()
            await release.wait()

    class Pool:
        @asynccontextmanager
        async def connection(self):
            yield Connection()
            finished.set()

    store = AgentPayloadStore(cast(AsyncConnectionPool, Pool()))
    task = asyncio.create_task(store.put(uuid4(), b"payload"))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()

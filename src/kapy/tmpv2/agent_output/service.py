"""Transient session broadcasts; no database access, replay, retries, or shared-client ownership.

Each publisher owns one bounded batch and at most one flush task. Each subscriber
borrows a dedicated PubSub connection until its context exits, even before its
iterator is first used. Channels are not isolated by Valkey database number.
"""

import asyncio
import logging
import math
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import cast
from uuid import UUID

from pydantic import TypeAdapter
from valkey.asyncio import ConnectionPool, Valkey
from valkey.asyncio.retry import Retry
from valkey.backoff import NoBackoff
from valkey.exceptions import ConnectionError as ValkeyConnectionError
from valkey.exceptions import TimeoutError as ValkeyTimeoutError

from kapy.tmpv2.agent_runner.types import MessageCommitted, OutputCallback, OutputEvent

logger = logging.getLogger(__name__)
_BATCH_ADAPTER = TypeAdapter(list[OutputEvent])
_MAX_BATCH_BYTES = 64 * 1024


class AgentOutputService:
    """Borrow an application-owned async Valkey client for ordinary PUBLISH/SUBSCRIBE.

    Use an environment-specific prefix. Cluster deployments need a directly
    addressable node client; this is not a sharded PubSub or ValkeyCluster API.
    """

    def __init__(self, client: Valkey, *, channel_prefix: str = "kapy:agent-output") -> None:
        self._client = client
        self._channel_prefix = channel_prefix

    @asynccontextmanager
    async def publisher(
        self, session_id: UUID, *, flush_interval: float = 0.5
    ) -> AsyncIterator[OutputCallback]:
        """Yield a session-bound callback; 0 disables batching, not publication.

        A finite nonnegative interval limits batch waiting. Normal exit flushes;
        exceptional exit drops pending deltas and joins the task. Transport failures
        drop the attempted batch, while cancellation and programming errors propagate.
        """
        if not math.isfinite(flush_interval) or flush_interval < 0:
            raise ValueError("flush_interval must be finite and nonnegative")
        publisher = _Publisher(self, session_id, flush_interval)
        try:
            yield publisher.write
        except BaseException:
            await publisher.close(failed=True)
            raise
        else:
            await publisher.close(failed=False)

    async def _publish(self, session_id: UUID, payload: bytes) -> None:
        # Send the command directly on a borrowed pool connection: client-level
        # retry settings must never turn an uncertain PUBLISH into a duplicate.
        pool = cast(ConnectionPool, self._client.connection_pool)
        try:
            async with asyncio.timeout(1.0):
                connection = await pool.get_connection("PUBLISH")
                try:
                    await connection.send_command(
                        "PUBLISH", f"{self._channel_prefix}:{session_id}", payload
                    )
                    await connection.read_response()
                except BaseException:
                    await connection.disconnect()
                    raise
                finally:
                    await pool.release(connection)
        except ValkeyConnectionError, ValkeyTimeoutError, TimeoutError:
            logger.warning("Dropped agent output batch for session %s", session_id, exc_info=True)

    @asynccontextmanager
    async def subscribe(self, session_id: UUID) -> AsyncIterator[AsyncIterator[OutputEvent]]:
        """Yield events only after SUBSCRIBE acknowledgement; errors end this subscription.

        Idle reads have no timeout. Disable retries on this exclusively borrowed
        connection, restoring its settings before returning it to the shared pool.
        Reconnecting requires a new history replay by the caller.
        """
        async with self._client.pubsub() as pubsub:
            async with asyncio.timeout(1.0):
                await pubsub.connect()
            connection = pubsub.connection
            assert connection is not None
            retry, socket_timeout = connection.retry, connection.socket_timeout
            connection.retry = Retry(NoBackoff(), 0)
            connection.socket_timeout = None
            try:
                async with asyncio.timeout(1.0):
                    await pubsub.subscribe(f"{self._channel_prefix}:{session_id}")
                    while True:
                        message = await pubsub.get_message(timeout=None)
                        if message is not None and message["type"] == "subscribe":
                            break

                async def events() -> AsyncGenerator[OutputEvent]:
                    async for message in pubsub.listen():
                        if message["type"] != "message":
                            continue
                        batch = _BATCH_ADAPTER.validate_json(message["data"])
                        if not batch:
                            raise ValueError("Output batches must not be empty")
                        for event in batch:
                            if _session_id(event) != session_id:
                                raise ValueError("Output event belongs to another session")
                            yield event

                iterator = events()
                try:
                    yield iterator
                finally:
                    await iterator.aclose()
            finally:
                connection.retry, connection.socket_timeout = retry, socket_timeout


def _session_id(event: OutputEvent) -> UUID:
    return event.message.session_id if isinstance(event, MessageCommitted) else event.session_id


class _Publisher:
    """One context's serialized sends and bounded buffer; never shared across sessions."""

    def __init__(self, service: AgentOutputService, session_id: UUID, interval: float) -> None:
        self._service = service
        self._session_id = session_id
        self._interval = interval
        self._buffer: list[bytes] = []
        self._size = 2  # JSON array brackets, plus encoded items and separators below.
        self._lock = asyncio.Lock()
        self._pending = asyncio.Event()
        self._stopping = asyncio.Event()
        self._closed = False
        self._task = (
            asyncio.create_task(self._run(), name=f"agent-output:{session_id}")
            if interval > 0
            else None
        )

    async def write(self, event: OutputEvent) -> None:
        if self._closed:
            raise RuntimeError("Output publisher is closed")
        if self._task is not None and self._task.done():
            self._task.result()
        if _session_id(event) != self._session_id:
            raise ValueError("Output event belongs to another session")
        encoded = _BATCH_ADAPTER.dump_json([event])[1:-1]
        async with self._lock:
            self._size += len(encoded) + bool(self._buffer)
            self._buffer.append(encoded)
            self._pending.set()
            if (
                self._interval == 0
                or self._size >= _MAX_BATCH_BYTES
                or isinstance(event, MessageCommitted)
            ):
                await self._flush()

    async def _flush(self) -> None:
        # All callers hold _lock, including during the bounded network attempt.
        if not self._buffer:
            return
        payload = b"[" + b",".join(self._buffer) + b"]"
        self._buffer.clear()
        self._size = 2
        self._pending.clear()
        await self._service._publish(self._session_id, payload)

    async def _run(self) -> None:
        while True:
            await self._pending.wait()
            try:
                async with asyncio.timeout(self._interval):
                    await self._stopping.wait()
            except TimeoutError:
                pass
            async with self._lock:
                await self._flush()
            if self._stopping.is_set():
                return

    async def close(self, *, failed: bool) -> None:
        self._closed = True
        task = self._task
        try:
            if task is not None:
                if failed:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                else:
                    self._stopping.set()
                    self._pending.set()
                    await task
            elif not failed:
                async with self._lock:
                    await self._flush()
        finally:
            self._buffer.clear()

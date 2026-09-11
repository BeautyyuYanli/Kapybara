"""Transient session broadcasts; no database access, replay, or shared-client ownership.

Each publisher owns one bounded buffer and one background send/recovery task. Each subscriber
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
from valkey.exceptions import AuthenticationError, AuthorizationError
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
        """Yield a fast, lossy callback; all network work runs in one background task.

        A finite nonnegative interval bounds batching; zero wakes the task at once.
        Ordinary callback/transport failures never reach the producer. Full buffers,
        recovery and closing discard events. Every exit cancels sending without a
        final flush; cancellation and errors in the context body still propagate.
        """
        if not math.isfinite(flush_interval) or flush_interval < 0:
            raise ValueError("flush_interval must be finite and nonnegative")
        publisher = _Publisher(self, session_id, flush_interval)
        try:
            yield publisher.write
        finally:
            await publisher.close()

    async def _command(self, *args: str | bytes) -> None:
        # Borrow independently of subscriber settings, with one deadline for the
        # connection, command and reply. Failed attempts never retain a connection.
        pool = cast(ConnectionPool, self._client.connection_pool)
        async with asyncio.timeout(1.0):
            connection = await pool.get_connection(args[0])
            try:
                await connection.send_command(*args)
                await connection.read_response()
            except BaseException:
                await connection.disconnect()
                raise
            finally:
                await pool.release(connection)

    async def _publish(self, session_id: UUID, payload: bytes) -> None:
        await self._command("PUBLISH", f"{self._channel_prefix}:{session_id}", payload)

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
    """One event-loop-local buffer plus at most one in-flight batch, each <= 64 KiB.

    Buffer mutations never await. Only the worker owns network I/O; recovery drops
    new events and probes independently of traffic. No failed batch is replayed.
    """

    def __init__(self, service: AgentOutputService, session_id: UUID, interval: float) -> None:
        self._service = service
        self._session_id = session_id
        self._interval = interval
        self._buffer: list[bytes] = []
        self._size = 2
        self._deadline = 0.0
        self._wake = asyncio.Event()
        self._closed = False
        self._accepting = True
        self._task = asyncio.create_task(self._run(), name=f"agent-output:{session_id}")

    async def write(self, event: OutputEvent) -> None:
        if self._closed or not self._accepting:
            return
        try:
            if _session_id(event) != self._session_id:
                raise ValueError("Output event belongs to another session")
            encoded = _BATCH_ADAPTER.dump_json([event])[1:-1]
            size = self._size + len(encoded) + bool(self._buffer)
            if size > _MAX_BATCH_BYTES:
                if self._buffer:
                    self._deadline = 0.0
                    self._wake.set()
                return
            if not self._buffer:
                self._deadline = asyncio.get_running_loop().time() + self._interval
            self._buffer.append(encoded)
            self._size = size
            if size == _MAX_BATCH_BYTES or isinstance(event, MessageCommitted):
                self._deadline = 0.0
            self._wake.set()
        except Exception as error:
            logger.warning("Dropped invalid agent output (%s)", type(error).__name__)

    def _clear(self) -> None:
        self._buffer.clear()
        self._size = 2

    async def _run(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                if not self._buffer:
                    continue
                delay = self._deadline - asyncio.get_running_loop().time()
                if delay > 0:
                    try:
                        async with asyncio.timeout(delay):
                            await self._wake.wait()
                    except TimeoutError:
                        pass
                    else:
                        continue
                payload = b"[" + b",".join(self._buffer) + b"]"
                self._clear()
                try:
                    await self._service._publish(self._session_id, payload)
                except AuthenticationError, AuthorizationError:
                    raise
                except ValkeyConnectionError, ValkeyTimeoutError, TimeoutError, OSError:
                    self._accepting = False
                    self._clear()
                    logger.warning("Agent output recovering for session %s", self._session_id)
                    await self._recover()
                    self._accepting = True
                    logger.info("Agent output recovered for session %s", self._session_id)
        except Exception as error:
            self._accepting = False
            self._clear()
            logger.warning("Agent output disabled (%s)", type(error).__name__)

    async def _recover(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            try:
                await self._service._command("PING")
            except AuthenticationError, AuthorizationError:
                raise
            except ValkeyConnectionError, ValkeyTimeoutError, TimeoutError, OSError:
                continue
            return

    async def close(self) -> None:
        self._closed = True
        self._clear()
        self._task.cancel()
        await asyncio.gather(self._task, return_exceptions=True)

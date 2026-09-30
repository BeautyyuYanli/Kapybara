"""Transient session broadcasts; no database access, replay, or shared-client ownership.

Each publisher owns one bounded buffer and one background send/recovery task. Each subscriber
owns one background receiver and a bounded, merged list of pending events.
The receiver owns its dedicated PubSub connection, including failure cleanup.
Channels are not isolated by Valkey database number.
"""

import asyncio
import logging
import math
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import cast
from uuid import UUID

from pydantic import TypeAdapter
from valkey.asyncio import ConnectionPool, Valkey
from valkey.asyncio.retry import Retry
from valkey.backoff import NoBackoff
from valkey.exceptions import AuthenticationError, AuthorizationError
from valkey.exceptions import ConnectionError as ValkeyConnectionError
from valkey.exceptions import TimeoutError as ValkeyTimeoutError

from kapy.agent_runner.types import MessageCommitted, OutputCallback, OutputEvent, TextDelta

logger = logging.getLogger(__name__)
_BATCH_ADAPTER = TypeAdapter(list[OutputEvent])
_MAX_BATCH_BYTES = 64 * 1024
_MAX_SUBSCRIBER_EVENTS = 256


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
        recovery and closing discard new events. Normal exit drains in-flight and
        buffered output within two seconds. Exceptional exit discards it; body
        exceptions and cancellation still propagate.
        """
        if not math.isfinite(flush_interval) or flush_interval < 0:
            raise ValueError("flush_interval must be finite and nonnegative")
        publisher = _Publisher(self, session_id, flush_interval)
        try:
            yield publisher.write
        except BaseException:
            await publisher.close(flush=False)
            raise
        else:
            await publisher.close(flush=True)

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
    async def subscribe(self, session_id: UUID) -> AsyncIterator[AsyncIterator[list[OutputEvent]]]:
        """Yield whole merged batches after acknowledgement, independently of network reads.

        One receiver owns the connection and releases it even while consumers pause.
        Keep at most 256 merged pending events, evicting the oldest entries after
        each merge regardless of type or authority. This bounds count, not memory;
        callers needing complete snapshots must recover missed output from history.
        Errors precede buffered data on the next read. Idle reads have no timeout or retry.
        The context cancels and joins its receiver even if iteration never starts.
        """
        ready, changed = asyncio.Event(), asyncio.Event()
        buffer: list[OutputEvent] = []
        failure: Exception | None = None
        finished = False

        async def receive() -> None:
            nonlocal buffer, failure, finished
            try:
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
                        ready.set()
                        async for message in pubsub.listen():
                            if message["type"] != "message":
                                continue
                            batch = _BATCH_ADAPTER.validate_json(message["data"])
                            if not batch:
                                raise ValueError("Output batches must not be empty")
                            for event in batch:
                                if _session_id(event) != session_id:
                                    raise ValueError("Output event belongs to another session")
                                candidate = _merge(buffer, event)
                                if len(candidate) > _MAX_SUBSCRIBER_EVENTS:
                                    del candidate[:-_MAX_SUBSCRIBER_EVENTS]
                                buffer = candidate
                            changed.set()
                    finally:
                        connection.retry, connection.socket_timeout = retry, socket_timeout
            except Exception as error:
                failure = error
            finally:
                finished = True
                ready.set()
                changed.set()

        async def batches() -> AsyncGenerator[list[OutputEvent]]:
            nonlocal buffer
            while True:
                if failure is not None:
                    raise failure
                if buffer:
                    batch, buffer = buffer, []
                    yield batch
                    continue
                if finished:
                    return
                # Checking state and clearing the signal do not await, so a writer
                # cannot slip between them and lose a wakeup.
                changed.clear()
                await changed.wait()

        task = asyncio.create_task(receive(), name=f"agent-output-subscribe:{session_id}")
        iterator = batches()
        try:
            await ready.wait()
            if failure is not None:
                raise failure
            yield iterator
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await iterator.aclose()
            buffer.clear()


def _session_id(event: OutputEvent) -> UUID:
    return event.message.session_id if isinstance(event, MessageCommitted) else event.session_id


def _message_key(event: OutputEvent) -> tuple[UUID, int]:
    if isinstance(event, MessageCommitted):
        return event.message.session_id, event.message.seq
    return event.session_id, event.response_seq


def _merge(buffer: list[OutputEvent], event: OutputEvent) -> list[OutputEvent]:
    """Build a candidate without mutating a buffer that may reject it for capacity.

    Only undelivered parts merge; replacing with empty text remains meaningful.
    Full snapshots replace only their own key, regardless of seq or authority.
    Later deltas form a new preview and never merge across a same-key snapshot.
    A merged event moves to the latest arrival position. No delivery cursor lives here.
    """
    if isinstance(event, MessageCommitted):
        return [item for item in buffer if _message_key(item) != _message_key(event)] + [event]
    candidate: list[OutputEvent] = []
    for item in buffer:
        if isinstance(item, TextDelta) and (
            _message_key(item),
            item.part_index,
            item.part_kind,
        ) == (
            _message_key(event),
            event.part_index,
            event.part_kind,
        ):
            if event.op == "append":
                event = replace(event, text=item.text + event.text, op=item.op)
        else:
            candidate.append(item)
    candidate.append(event)
    return candidate


class _Publisher:
    """One event-loop-local buffer plus at most one in-flight batch, each <= 64 KiB.

    Buffer mutations never await. Only the worker owns network I/O; recovery drops
    new events and probes independently of traffic. No failed batch is replayed.
    """

    def __init__(self, service: AgentOutputService, session_id: UUID, interval: float) -> None:
        self._service = service
        self._session_id = session_id
        self._interval = interval
        self._buffer: list[OutputEvent] = []
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
            candidate = _merge(self._buffer, event)
            size = len(_BATCH_ADAPTER.dump_json(candidate))
            if size > _MAX_BATCH_BYTES:
                if self._buffer:
                    self._deadline = 0.0
                    self._wake.set()
                return
            if not self._buffer:
                self._deadline = asyncio.get_running_loop().time() + self._interval
            self._buffer = candidate
            if size == _MAX_BATCH_BYTES or isinstance(event, MessageCommitted):
                self._deadline = 0.0
            self._wake.set()
        except Exception as error:
            logger.warning("Dropped invalid agent output (%s)", type(error).__name__)

    async def _run(self) -> None:
        try:
            while not self._closed or self._buffer:
                await self._wake.wait()
                self._wake.clear()
                if not self._buffer:
                    continue
                delay = 0.0 if self._closed else self._deadline - asyncio.get_running_loop().time()
                if delay > 0:
                    try:
                        async with asyncio.timeout(delay):
                            await self._wake.wait()
                    except TimeoutError:
                        pass
                    else:
                        continue
                batch, self._buffer = self._buffer, []
                payload = _BATCH_ADAPTER.dump_json(batch)
                try:
                    await self._service._publish(self._session_id, payload)
                except AuthenticationError, AuthorizationError:
                    raise
                except ValkeyConnectionError, ValkeyTimeoutError, TimeoutError, OSError:
                    if self._closed:
                        raise
                    self._accepting = False
                    self._buffer.clear()
                    logger.warning("Agent output recovering for session %s", self._session_id)
                    await self._recover()
                    self._accepting = True
                    logger.info("Agent output recovered for session %s", self._session_id)
        except Exception as error:
            self._accepting = False
            self._buffer.clear()
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

    async def close(self, *, flush: bool) -> None:
        self._closed = True
        self._wake.set()
        try:
            if flush and self._accepting:
                async with asyncio.timeout(2.0):
                    await asyncio.shield(self._task)
        except TimeoutError:
            logger.warning("Agent output close timed out for session %s", self._session_id)
        finally:
            self._buffer.clear()
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

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
_MAX_SUBSCRIBER_BYTES = 1024 * 1024


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
    async def subscribe(self, session_id: UUID) -> AsyncIterator[AsyncIterator[list[OutputEvent]]]:
        """Yield whole merged batches after acknowledgement, independently of network reads.

        One receiver owns the connection and releases it even while consumers pause.
        Pending output is bounded to 1 MiB of JSON; overflowing deltas are dropped,
        but commits that cannot fit without deltas fail with BufferError. Errors
        precede buffered data on the next read. Idle reads have no timeout or retry.
        The context cancels and joins its receiver even if iteration never starts.
        """
        ready, changed = asyncio.Event(), asyncio.Event()
        buffer: list[OutputEvent] = []
        complete_seq = -1
        failure: Exception | None = None
        finished = False

        async def receive() -> None:
            nonlocal buffer, complete_seq, failure, finished
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
                                candidate, complete_seq = _merge(buffer, event, complete_seq)
                                if len(_BATCH_ADAPTER.dump_json(candidate)) > _MAX_SUBSCRIBER_BYTES:
                                    if isinstance(event, TextDelta):
                                        continue
                                    commits: list[OutputEvent] = [
                                        item
                                        for item in candidate
                                        if isinstance(item, MessageCommitted)
                                    ]
                                    candidate = commits
                                    if (
                                        len(_BATCH_ADAPTER.dump_json(candidate))
                                        > _MAX_SUBSCRIBER_BYTES
                                    ):
                                        raise BufferError("Output subscription buffer is full")
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


def _merge(
    buffer: list[OutputEvent], event: OutputEvent, complete_seq: int
) -> tuple[list[OutputEvent], int]:
    """Build a candidate without mutating a buffer that may reject it for capacity.

    Only undelivered parts merge; replacing with empty text remains meaningful.
    Moving a merged part to the tail preserves the latest arrival position, while
    complete messages retain their relative order. No delivery cursor lives here.
    """
    if isinstance(event, MessageCommitted):
        complete_seq = max(complete_seq, event.message.seq)
        return [
            item
            for item in buffer
            if isinstance(item, MessageCommitted) or item.response_seq > complete_seq
        ] + [event], complete_seq
    if event.response_seq <= complete_seq:
        return buffer, complete_seq
    candidate: list[OutputEvent] = []
    for item in buffer:
        if isinstance(item, TextDelta) and (item.response_seq, item.part_index, item.part_kind) == (
            event.response_seq,
            event.part_index,
            event.part_kind,
        ):
            if event.op == "append":
                event = replace(event, text=item.text + event.text, op=item.op)
        else:
            candidate.append(item)
    candidate.append(event)
    return candidate, complete_seq


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
            self._buffer.append(event)
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
                batch, self._buffer = self._buffer, []
                self._clear()
                merged: list[OutputEvent] = []
                complete_seq = -1
                for event in batch:
                    merged, complete_seq = _merge(merged, event, complete_seq)
                if not merged:
                    continue
                payload = _BATCH_ADAPTER.dump_json(merged)
                if len(payload) > _MAX_BATCH_BYTES:
                    continue
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

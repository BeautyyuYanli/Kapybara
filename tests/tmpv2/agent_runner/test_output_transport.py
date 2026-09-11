"""Real Valkey contracts for batching, broadcast, subscription ownership and failures."""

import asyncio
import os
from typing import Literal
from uuid import uuid4

import pytest
from pydantic import TypeAdapter
from pydantic_ai.messages import ModelResponse, TextPart
from valkey.asyncio import Valkey
from valkey.asyncio.connection import Connection
from valkey.asyncio.retry import Retry
from valkey.backoff import NoBackoff
from valkey.exceptions import ConnectionError as ValkeyConnectionError

from kapy.tmpv2.agent_output import AgentOutputService
from kapy.tmpv2.agent_runner import HistoryMessage, MessageCommitted, OutputEvent, TextDelta

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
ADAPTER = TypeAdapter(list[OutputEvent])


def delta(session_id, text="hello", *, op: Literal["replace", "append"] = "append"):
    return TextDelta(session_id, 1, 0, "text", op, text)


async def test_ready_subscription_broadcast_batch_order_and_cleanup(valkey_client):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    first = delta(session_id, "hello", op="replace")
    second = delta(session_id, " world")
    committed = MessageCommitted(
        HistoryMessage(session_id, 1, ModelResponse(parts=[TextPart("hello world")]))
    )
    channel = f"kapy:agent-output:{session_id}"
    async with outputs.subscribe(session_id) as left, outputs.subscribe(session_id) as right:
        assert (await valkey_client.pubsub_numsub(channel))[0][1] == 2
        async with outputs.publisher(session_id, flush_interval=60) as callback:
            await callback(first)
            await callback(second)
            await callback(committed)
            async with asyncio.timeout(2):
                assert [await anext(left) for _ in range(3)] == [first, second, committed]
                assert [await anext(right) for _ in range(3)] == [first, second, committed]
    assert (await valkey_client.pubsub_numsub(channel))[0][1] == 0
    assert await valkey_client.ping()


async def test_interval_batches_once_without_waiting_for_next_event(valkey_client):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    async with valkey_client.pubsub() as raw:
        await raw.subscribe(f"kapy:agent-output:{session_id}")
        assert (await raw.get_message(timeout=1))["type"] == "subscribe"
        async with outputs.publisher(session_id, flush_interval=0.1) as callback:
            await callback(delta(session_id, "one"))
            await callback(delta(session_id, "two"))
            assert await raw.get_message(timeout=0.02) is None
            async with asyncio.timeout(2):
                message = await raw.get_message(timeout=None)
            assert ADAPTER.validate_json(message["data"]) == [
                delta(session_id, "one"),
                delta(session_id, "two"),
            ]
            assert await raw.get_message(timeout=0.02) is None


@pytest.mark.parametrize(
    "interval,texts",
    [(0, ["immediate"]), (60, ["甲" * 12_000, "乙" * 12_000])],
)
async def test_immediate_and_encoded_size_flush(valkey_client, interval, texts):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    expected = [delta(session_id, text) for text in texts]
    async with valkey_client.pubsub() as raw:
        await raw.subscribe(f"kapy:agent-output:{session_id}")
        await raw.get_message(timeout=1)
        async with outputs.publisher(session_id, flush_interval=interval) as callback:
            for event in expected:
                await callback(event)
            async with asyncio.timeout(1):
                message = await raw.get_message(timeout=None)
            assert ADAPTER.validate_json(message["data"]) == expected[:1]


@pytest.mark.parametrize("failed", [False, True])
async def test_exit_always_drops_pending_output(valkey_client, failed):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    async with valkey_client.pubsub() as raw:
        await raw.subscribe(f"kapy:agent-output:{session_id}")
        await raw.get_message(timeout=1)
        callback = None
        try:
            async with outputs.publisher(session_id, flush_interval=60) as callback:
                await callback(delta(session_id))
                if failed:
                    raise RuntimeError("business failure")
        except RuntimeError as error:
            assert str(error) == "business failure"
        message = await raw.get_message(timeout=0.05)
        assert message is None
        assert callback is not None
        await callback(delta(session_id))  # Closed callbacks remain harmless.
    assert not any(task.get_name() == f"agent-output:{session_id}" for task in asyncio.all_tasks())


async def test_no_subscriber_drops_previous_messages(valkey_client):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    async with outputs.publisher(session_id, flush_interval=0) as callback:
        await callback(delta(session_id, "lost"))
        await asyncio.sleep(0.05)
        async with outputs.subscribe(session_id) as events:
            await callback(delta(session_id, "live"))
            async with asyncio.timeout(1):
                assert await anext(events) == delta(session_id, "live")


async def test_unstarted_iterator_and_cancelled_listener_release_subscription(valkey_client):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    async with outputs.subscribe(session_id):
        pass
    channel = f"kapy:agent-output:{session_id}"
    assert (await valkey_client.pubsub_numsub(channel))[0][1] == 0
    ready = asyncio.Event()

    async def listen():
        async with outputs.subscribe(session_id) as events:
            ready.set()
            await anext(events)

    task = asyncio.create_task(listen())
    await ready.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await valkey_client.pubsub_numsub(channel))[0][1] == 0
    assert await valkey_client.ping()


async def test_cancelled_publisher_discards_buffer_and_joins_task(valkey_client):
    session_id = uuid4()
    outputs = AgentOutputService(valkey_client)
    ready = asyncio.Event()

    async def produce():
        async with outputs.publisher(session_id, flush_interval=60) as callback:
            await callback(delta(session_id))
            ready.set()
            await asyncio.Event().wait()

    async with valkey_client.pubsub() as raw:
        await raw.subscribe(f"kapy:agent-output:{session_id}")
        await raw.get_message(timeout=1)
        task = asyncio.create_task(produce())
        await ready.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await raw.get_message(timeout=0.05) is None
    assert not any(task.get_name() == f"agent-output:{session_id}" for task in asyncio.all_tasks())


async def test_transport_failure_and_bad_session_are_dropped(caplog):
    # Port 0 cannot host a remote TCP service; exercise the real client's connect failure.
    async with Valkey(host="127.0.0.1", port=0, socket_connect_timeout=0.1) as client:
        session_id = uuid4()
        outputs = AgentOutputService(client)
        async with outputs.publisher(session_id, flush_interval=0) as callback:
            await callback(delta(session_id))
            await callback(delta(uuid4()))
            await asyncio.sleep(0.05)
        assert "Agent output recovering" in caplog.text


@pytest.mark.parametrize("reply_fault", ["delay", "disconnect"])
async def test_publish_deadline_and_no_retry_after_lost_confirmation(
    valkey_client, caplog, reply_fault
):
    session_id = uuid4()
    armed = asyncio.Event()
    confirmation_lost = asyncio.Event()

    class LoseOneReply(Connection):
        async def read_response(self, *args, **kwargs):
            reply = await super().read_response(*args, **kwargs)
            if armed.is_set():
                # The real server executed PUBLISH. Suppress only this connection's
                # first confirmation, leaving reconnect handshakes and other clients intact.
                armed.clear()
                confirmation_lost.set()
                assert reply == 1
                if reply_fault == "delay":
                    await asyncio.Event().wait()
                raise ValkeyConnectionError("Test connection lost its PUBLISH confirmation")
            return reply

    async with (
        Valkey.from_url(
            os.environ.get("KAPY_VALKEY_URL", "valkey://127.0.0.1:56379/0"),
            connection_class=LoseOneReply,
            max_connections=1,
            socket_timeout=None,
            retry=Retry(NoBackoff(), 3),
            retry_on_error=[ValkeyConnectionError],
        ) as client,
        valkey_client.pubsub() as raw,
    ):
        await client.ping()  # Warm this exclusive connection before arming the fault.
        await raw.subscribe(f"kapy:agent-output:{session_id}")
        await raw.get_message(timeout=1)
        armed.set()
        async with AgentOutputService(client).publisher(session_id, flush_interval=0) as callback:
            async with asyncio.timeout(2):
                await callback(delta(session_id))
                await confirmation_lost.wait()
                await asyncio.sleep(1.1 if reply_fault == "delay" else 0.05)
        assert confirmation_lost.is_set()
        message = await raw.get_message(timeout=1)
        assert ADAPTER.validate_json(message["data"]) == [delta(session_id)]
        assert await raw.get_message(timeout=0.05) is None  # No duplicate after uncertain delivery.
        assert sum("Agent output recovering" in record.message for record in caplog.records) == 1
        assert await client.ping()


async def test_subscription_disconnect_raises_instead_of_resuming(valkey_client):
    outputs = AgentOutputService(valkey_client)
    session_id = uuid4()
    # A dedicated client name identifies this test's borrowed subscriber connection.
    async with Valkey.from_url(
        os.environ.get("KAPY_VALKEY_URL", "valkey://127.0.0.1:56379/0"),
        client_name=f"output-test:{session_id}",
        retry=Retry(NoBackoff(), 3),
        retry_on_error=[ValkeyConnectionError],
        socket_timeout=0.1,
    ) as subscriber_client:
        outputs = AgentOutputService(subscriber_client)
        async with outputs.subscribe(session_id) as events:
            # The subscriber must override the shared client socket timeout, too.
            waiting = asyncio.ensure_future(anext(events))
            try:
                await asyncio.sleep(0.15)
                assert not waiting.done()
                client_id = next(
                    item["id"]
                    for item in await valkey_client.client_list()
                    if item["name"] == f"output-test:{session_id}"
                )
                await valkey_client.client_kill_filter(_id=client_id)
                async with asyncio.timeout(1):
                    with pytest.raises(ValkeyConnectionError):
                        await waiting
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)


@pytest.mark.parametrize("interval", [-1, float("nan"), float("inf")])
async def test_invalid_interval_rejected_without_connecting(interval):
    async with Valkey(host="127.0.0.1", port=0) as client:
        with pytest.raises(ValueError, match="finite and nonnegative"):
            async with AgentOutputService(client).publisher(uuid4(), flush_interval=interval):
                pass


async def test_blocked_network_never_blocks_callback_and_buffers_are_bounded(
    valkey_client, monkeypatch
):
    outputs = AgentOutputService(valkey_client)
    session_id = uuid4()
    sending = asyncio.Event()
    release = asyncio.Event()
    sent = asyncio.Event()
    batches = []

    async def blocked_publish(requested_session, payload):
        assert requested_session == session_id
        batches.append(payload)
        sending.set()
        await release.wait()
        sent.set()

    monkeypatch.setattr(outputs, "_publish", blocked_publish)
    async with outputs.publisher(session_id, flush_interval=0) as callback:
        await callback(delta(session_id, "in flight"))
        await sending.wait()
        # A callback must complete without even yielding to a network worker.
        async with asyncio.timeout(0.1):
            for _ in range(100):
                await callback(delta(session_id, "界" * 12_000))
            await callback(delta(session_id, "界" * 30_000))
            await callback(MessageCommitted(HistoryMessage(session_id, 1, ModelResponse(parts=[]))))
        release.set()
        await sent.wait()
    assert all(len(payload) <= 64 * 1024 for payload in batches)
    assert len(batches) <= 2
    assert not any(task.get_name() == f"agent-output:{session_id}" for task in asyncio.all_tasks())


async def test_publisher_recovers_without_new_events_and_discards_failed_batch(valkey_client):
    session_id = uuid4()
    failed = asyncio.Event()
    recovered = asyncio.Event()
    armed = False

    class FailOnePublish(Connection):
        async def send_command(self, *args, **kwargs):
            nonlocal armed
            if armed and args[0] == "PUBLISH":
                armed = False
                failed.set()
                raise ValkeyConnectionError("injected temporary failure")
            await super().send_command(*args, **kwargs)
            if failed.is_set() and args[0] == "PING":
                recovered.set()

    async with (
        Valkey.from_url(
            os.environ.get("KAPY_VALKEY_URL", "valkey://127.0.0.1:56379/0"),
            connection_class=FailOnePublish,
            max_connections=1,
        ) as client,
        valkey_client.pubsub() as raw,
    ):
        await client.ping()
        await raw.subscribe(f"kapy:agent-output:{session_id}")
        await raw.get_message(timeout=1)
        armed = True
        async with AgentOutputService(client).publisher(session_id, flush_interval=0) as callback:
            await callback(delta(session_id, "failed"))
            await failed.wait()
            # Allow failure cleanup to enter recovery, then lose another event.
            await asyncio.sleep(0.05)
            await callback(delta(session_id, "during recovery"))
            async with asyncio.timeout(3):
                await recovered.wait()
                # The actual PING reply must finish before accepting new output.
                await asyncio.sleep(0.05)
                await callback(delta(session_id, "after recovery"))
                message = await raw.get_message(timeout=None)
            assert ADAPTER.validate_json(message["data"]) == [delta(session_id, "after recovery")]
            assert await raw.get_message(timeout=0.05) is None
        assert await client.ping()


async def test_unrecoverable_publish_error_disables_without_reaching_producer(
    valkey_client, monkeypatch, caplog
):
    from valkey.exceptions import AuthenticationError

    outputs = AgentOutputService(valkey_client)
    called = asyncio.Event()

    async def denied(*args):
        called.set()
        raise AuthenticationError("secret must not appear in log")

    monkeypatch.setattr(outputs, "_publish", denied)
    session_id = uuid4()
    async with outputs.publisher(session_id, flush_interval=0) as callback:
        await callback(delta(session_id))
        await called.wait()
        await callback(delta(session_id))
    assert "Agent output disabled (AuthenticationError)" in caplog.text
    assert "secret must not appear" not in caplog.text

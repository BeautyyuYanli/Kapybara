"""Sequential tools share State; completed node boundaries survive later failure."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import UUID, uuid4

import httpx2
import pytest
from httpcore2 import AsyncConnectionPool
from pydantic_ai.exceptions import ModelRetry, UserError
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ToolCallPart,
    ToolReturnPart,
)
from shellctl import ShellctlClient
from shellctl_fixture import shell_server as shell_server
from temporalio import activity
from temporalio.client import ActivityFailureError
from temporalio.exceptions import CancelledError
from temporalio.worker import Worker

from kapy.application.resources import connect_temporal
from kapy.control.models.models import ModelRow, ProviderRow
from kapy.control.sessions import CreateSession, SessionService
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.layers.shell import runtime
from kapy.lifecycle import LifecycleStatus
from kapy.runner.activities import RunnerActivity

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.skipif(
        "KAPY_SHELLCTL_TEST_BIN_DIR" not in os.environ,
        reason="Set KAPY_SHELLCTL_TEST_BIN_DIR in a container with tmux and shellctl binaries",
    ),
]


def metadata(observation):
    return json.loads(observation.split("<metadata>\n", 1)[1].split("\n</metadata>", 1)[0])


def prompt(*steps, fail=False):
    return json.dumps({"steps": [list(step) for step in steps], "fail": fail})


def run(script, timeout=0.2):
    return {"tool": "builtin_shell_run", "args": {"script": script, "timeout": timeout}}


@asynccontextmanager
async def worker_service(database, **worker_options):
    client = await connect_temporal(database.settings)
    queue = f"layers-{uuid4()}"
    runner = RunnerActivity(database.sessions, database.outputs, client)
    async with Worker(client, task_queue=queue, activities=[runner.run], **worker_options):
        yield SessionService(database.sessions, temporal_client=client, temporal_task_queue=queue)


async def create_shell_session(database, service, shell_server):
    client, path = shell_server
    async with database.sessions.begin() as db:
        provider = ProviderRow(
            name="layer-test",
            api_key="fake-key",
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="runner.layer_model:LayerModel",
        )
        db.add(provider)
        db.add(
            ModelRow(
                provider_id=provider.id, model_name="layer-test", name="test", context_window=100000
            )
        )
    return await service.create_session(
        CreateSession(
            provider_id=provider.id,
            model_name="layer-test",
            layers={"shell": {"base_url": str(client.base_url), "cwd": str(path)}},
        )
    )


async def state(database, session_id):
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        return row.layer_states["shell"]


async def test_two_sequential_tools_resume_and_close_preserves_remote_jobs(
    runner_database, shell_server
):
    client, _ = shell_server
    async with worker_service(runner_database) as service:
        session = await create_shell_session(runner_database, service, shell_server)
        assert "layer_states" not in session.model_dump()
        assert await state(runner_database, session.id) == {"jobs": {}}
        first = await service.start_runner(
            session.id,
            user_prompt=prompt(
                (
                    run(
                        "printf 'first\\n'; read -r answer; "
                        "printf 'echo:%s\\n' \"$answer\"; sleep 30"
                    ),
                    run("printf 'other\\n'; sleep 30"),
                )
            ),
            activity_id=uuid4(),
        )
        outputs = json.loads(await first.result())
        assert "first" in outputs[0]
        job_a, job_b = [metadata(output)["job_id"] for output in outputs]
        saved = await state(runner_database, session.id)
        assert "scope_id" not in saved
        assert set(saved["jobs"]) == {job_a, job_b} and all(
            offset > 0 for offset in saved["jobs"].values()
        )
        # The second run restores the committed cursors for the same remote jobs.
        second = await service.start_runner(
            session.id,
            user_prompt=prompt(
                (
                    {
                        "tool": "builtin_shell_input",
                        "args": {"job_id": job_a, "text": "resume\n", "timeout": 2},
                    },
                )
            ),
            activity_id=uuid4(),
        )
        assert "echo:resume" in await second.result()
        resumed = await state(runner_database, session.id)
        assert "scope_id" not in resumed
        assert resumed["jobs"][job_a] > saved["jobs"][job_a]
        assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
        assert await state(runner_database, session.id) == resumed
        for job_id in (job_a, job_b):
            assert (await client.status(job_id)).job_id == job_id


async def test_model_failure_preserves_completed_tool_snapshot(runner_database, shell_server):
    async with worker_service(runner_database) as service:
        session = await create_shell_session(runner_database, service, shell_server)
        first = await service.start_runner(
            session.id,
            user_prompt=prompt((run("printf 'first\\n'; sleep 30"),)),
            activity_id=uuid4(),
        )
        assert "first" in await first.result()
        before = await state(runner_database, session.id)
        handle = await service.start_runner(
            session.id,
            user_prompt=prompt((run("printf 'saved\\n'; sleep 30"),), fail=True),
            activity_id=uuid4(),
        )
        with pytest.raises(ActivityFailureError):
            await handle.result()
        saved = await state(runner_database, session.id)
        assert "scope_id" not in saved
        assert set(saved["jobs"]) > set(before["jobs"])
        current = await service.get_session(session.id)
        assert current.status == LifecycleStatus.READY
        assert current.current_activity_id is current.current_activity_run_id is None
        assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED


@pytest.mark.parametrize("signal", [ModelRetry, UserError])
async def test_tool_signal_keeps_mutated_local_state_until_run_outcome(
    runner_database, shell_server, monkeypatch, signal
):
    client, _ = shell_server
    original = runtime.ShellOperations._result
    jobs = []

    async def retry_once(self, result):
        jobs.append(result.job_id)
        if len(jobs) == 1:
            raise signal("try another shell job")
        return await original(self, result)

    monkeypatch.setattr(runtime.ShellOperations, "_result", retry_once)
    async with worker_service(runner_database) as service:
        session = await create_shell_session(runner_database, service, shell_server)
        handle = await service.start_runner(
            session.id,
            user_prompt=prompt((run("printf 'retry\\n'; sleep 30"),)),
            activity_id=uuid4(),
        )
        if signal is ModelRetry:
            assert "retry" in await handle.result()
        else:
            with pytest.raises(ActivityFailureError):
                await handle.result()
        saved = await state(runner_database, session.id)
        assert "scope_id" not in saved
        if signal is ModelRetry:
            assert list(saved["jobs"]) == jobs and len(jobs) == 2
            assert saved["jobs"][jobs[1]] > 0
        else:
            assert saved["jobs"] == {} and len(jobs) == 1
        # State failure does not roll back the already-created remote job.
        assert (await client.status(jobs[0])).job_id == jobs[0]
        await service.close_session(session.id)


async def test_resume_after_cancellation_recovers_snapshot_without_replaying_the_tool(
    runner_database, shell_server, monkeypatch
):
    remote, _ = shell_server
    registered = asyncio.Event()
    cleanup_started, release_cleanup, cleanup_finished = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    closing_client: ShellctlClient | None = None
    original_close = ShellctlClient.close
    original_result = runtime.ShellOperations._result

    async def blocked_result(self, result):
        nonlocal closing_client
        if closing_client is not None:
            return await original_result(self, result)
        assert activity.in_activity()
        closing_client = self.client
        registered.set()
        await asyncio.Future()

    async def delayed_close(client: ShellctlClient) -> None:
        # Gate only this Activity's real client; the fixture client closes normally.
        if client is closing_client:
            assert activity.in_activity()
            cleanup_started.set()
            try:
                await release_cleanup.wait()
            finally:
                await original_close(client)
                cleanup_finished.set()
        else:
            await original_close(client)

    monkeypatch.setattr(runtime.ShellOperations, "_result", blocked_result)
    monkeypatch.setattr(ShellctlClient, "close", delayed_close)
    async with worker_service(runner_database) as service:
        session = await create_shell_session(runner_database, service, shell_server)
        handle = await service.start_runner(
            session.id,
            user_prompt=prompt((run("printf 'cancel\\n'; sleep 30"),)),
            activity_id=uuid4(),
        )
        result = asyncio.create_task(handle.result())
        try:
            await asyncio.wait_for(registered.wait(), 10)
            await handle.cancel()
            await asyncio.wait_for(cleanup_started.wait(), 10)
            # Cancellation must wait for resource cleanup before completing the Activity.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(result), 0.5)
            current = await service.get_session(session.id)
            assert handle.run_id is not None
            assert (current.current_activity_id, current.current_activity_run_id) == (
                UUID(handle.id),
                handle.run_id,
            )
            release_cleanup.set()
            with pytest.raises(ActivityFailureError) as cancelled:
                await asyncio.wait_for(result, 20)
            assert isinstance(cancelled.value.cause, CancelledError)
            await asyncio.wait_for(cleanup_finished.wait(), 5)
            saved = await state(runner_database, session.id)
            assert saved == {"jobs": {}}
            current = await service.get_session(session.id)
            assert current.status == LifecycleStatus.READY
            assert current.current_activity_id is current.current_activity_run_id is None

            async with runner_database.sessions.begin() as db:
                snapshot = await SessionRepository(db).read_runner_state(session.id)
            assert snapshot is not None
            messages = ModelMessagesTypeAdapter.validate_json(snapshot)
            assert isinstance(messages[-1], ModelRequest) and messages[-1].state == "interrupted"
            (call,) = [
                part
                for message in messages
                for part in message.parts
                if isinstance(part, ToolCallPart)
            ]
            (job,) = await remote.list_jobs()
            resumed = await service.start_runner(session.id, activity_id=uuid4(), user_prompt=None)
            output = await asyncio.wait_for(resumed.result(), 15)
            (receipt,) = [
                part
                for entry in (await service.read_history(session.id)).items
                for part in entry.message.parts
                if isinstance(part, ToolReturnPart)
            ]
            assert receipt.tool_call_id == call.tool_call_id and receipt.outcome == "interrupted"
            assert json.loads(output) == [receipt.content]
            assert [current.job_id for current in await remote.list_jobs()] == [job.job_id]
            assert await state(runner_database, session.id) == {"jobs": {}}
        finally:
            release_cleanup.set()
            if not result.done():
                await handle.cancel()
            await asyncio.wait_for(asyncio.gather(result, return_exceptions=True), 20)
            await service.close_session(session.id)


async def test_cancel_during_shell_close_drains_http_connections(
    runner_database, shell_server, monkeypatch
):
    close_started = asyncio.Event()
    cancellation_received = asyncio.Event()
    release_close = asyncio.Event()
    connections = []
    original_close = ShellctlClient.close

    async def delayed_close(client: ShellctlClient) -> None:
        if not activity.in_activity():
            await original_close(client)
            return

        # Keep the first response open while the second request opens another connection.
        async with client._client.stream("GET", "/healthz") as response:
            response.raise_for_status()
            await client.health()
            await response.aread()
        transport = client._client._transport
        assert isinstance(transport, httpx2.AsyncHTTPTransport)
        pool = transport._pool
        assert isinstance(pool, AsyncConnectionPool)
        connections.extend(pool.connections)
        assert len(connections) == 2
        first = connections[0]
        close_first = first.aclose

        async def gated_close() -> None:
            close_started.set()
            try:
                await activity.wait_for_cancelled()
            finally:
                cancellation_received.set()
            await release_close.wait()
            await close_first()

        with monkeypatch.context() as patch:
            patch.setattr(first, "aclose", gated_close)
            await original_close(client)

    monkeypatch.setattr(ShellctlClient, "close", delayed_close)
    # Deliver the Temporal cancellation within the client's five-second close budget.
    async with worker_service(
        runner_database, max_heartbeat_throttle_interval=timedelta(seconds=1)
    ) as service:
        session = await create_shell_session(runner_database, service, shell_server)
        handle = await service.start_runner(
            session.id, user_prompt=prompt((run("printf 'done'"),)), activity_id=uuid4()
        )
        result = asyncio.create_task(handle.result())
        cancel_requested = False
        try:
            await asyncio.wait_for(close_started.wait(), 10)
            cancel_requested = True
            await handle.cancel()
            await asyncio.wait_for(cancellation_received.wait(), 5)
            current = await service.get_session(session.id)
            assert handle.run_id is not None
            assert (current.current_activity_id, current.current_activity_run_id) == (
                UUID(handle.id),
                handle.run_id,
            )
            assert not result.done()

            release_close.set()
            with pytest.raises(ActivityFailureError) as cancelled:
                await asyncio.wait_for(result, 20)
            assert isinstance(cancelled.value.cause, CancelledError)
            assert all(connection.is_closed() for connection in connections)
            current = await service.get_session(session.id)
            assert current.status == LifecycleStatus.READY
            assert current.current_activity_id is current.current_activity_run_id is None
        finally:
            release_close.set()
            if not cancel_requested and not result.done():
                await handle.cancel()
            await asyncio.wait_for(asyncio.gather(result, return_exceptions=True), 20)
            for connection in connections:
                if not connection.is_closed():
                    await connection.aclose()
            await service.close_session(session.id)

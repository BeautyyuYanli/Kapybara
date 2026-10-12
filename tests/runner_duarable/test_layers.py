"""Successful tool State crosses Temporal; only final run snapshots reach PostgreSQL."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic_ai.durable_exec.temporal import AgentPlugin, PydanticAIPlugin
from pydantic_ai.exceptions import ModelRetry, UserError
from shellctl import ShellctlClient, ShellctlClientError
from shellctl.shared.constants import SHELL_TOOL_HARD_TIMEOUT_SECONDS
from shellctl_fixture import shell_server as shell_server
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.worker import Replayer, Worker

from kapy.application.resources import connect_temporal
from kapy.control.models.models import ModelRow, ProviderRow
from kapy.control.sessions import CreateSession, SessionService
from kapy.control.sessions.models import SessionRow
from kapy.layers.shell import runtime
from kapy.lifecycle import LifecycleStatus
from kapy.runner_duarable import RunnerWorkflow, agent
from kapy.runner_duarable.activities import RunnerActivities

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
async def worker_service(database):
    client = await connect_temporal(database.settings)
    queue = f"layers-{uuid4()}"
    activities = RunnerActivities(database.sessions, database.outputs)
    async with Worker(
        client,
        task_queue=queue,
        workflows=[RunnerWorkflow],
        activities=[
            activities.record_messages,
            activities.save_runner_state,
        ],
        plugins=[AgentPlugin(agent)],
    ):
        yield SessionService(database.sessions, temporal_client=client, temporal_task_queue=queue)


async def create_shell_session(database, service, shell_server):
    client, path = shell_server
    async with database.sessions.begin() as db:
        provider = ProviderRow(
            name="layer-test",
            api_key="fake-key",
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="runner_duarable.layer_model:LayerModel",
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
        return row.layer_states["shell"], row.runner_state_version


async def test_two_tools_resume_replay_and_partial_close(
    runner_database, shell_server, monkeypatch
):
    client, _ = shell_server
    async with worker_service(runner_database) as service:
        session = await create_shell_session(runner_database, service, shell_server)
        assert "layer_states" not in session.model_dump()
        assert (await state(runner_database, session.id)) == (
            {"scope_id": str(session.id), "jobs": {}},
            0,
        )
        first = await service.start_durable_runner(
            session.id,
            user_prompt=prompt(
                (
                    run(
                        "printf 'ready\\n'; read -r answer; "
                        "printf 'echo:%s\\n' \"$answer\"; sleep 30"
                    ),
                    run("printf 'other\\n'; sleep 30"),
                )
            ),
        )
        outputs = json.loads(await first.result())
        job_a, job_b = [metadata(output)["job_id"] for output in outputs]
        saved, version = await state(runner_database, session.id)
        assert saved["scope_id"] == str(session.id)
        assert set(saved["jobs"]) == {job_a, job_b} and all(
            offset > 0 for offset in saved["jobs"].values()
        )
        assert version == 1
        history = await first.fetch_history()
        # A second Workflow gets the committed cursor instead of Activity-local defaults.
        second = await service.start_durable_runner(
            session.id,
            user_prompt=prompt(
                (
                    {
                        "tool": "builtin_shell_input",
                        "args": {"job_id": job_a, "text": "resume\n", "timeout": 2},
                    },
                )
            ),
        )
        assert "echo:resume" in await second.result()
        resumed, version = await state(runner_database, session.id)
        assert resumed["scope_id"] == str(session.id) and version == 2
        assert resumed["jobs"][job_a] > saved["jobs"][job_a]
        expected_activities = {
            f"agent__runner_duarable__dynamic_toolset__shell__{operation}"
            for operation in ("get_tools", "call_tool")
        }
        shell_activities = [
            event.activity_task_scheduled_event_attributes
            for event in history.events
            if event.HasField("activity_task_scheduled_event_attributes")
            and event.activity_task_scheduled_event_attributes.activity_type.name
            in expected_activities
        ]
        assert {attrs.activity_type.name for attrs in shell_activities} == expected_activities
        for attrs in shell_activities:
            assert attrs.start_to_close_timeout.ToTimedelta() == timedelta(
                seconds=SHELL_TOOL_HARD_TIMEOUT_SECONDS + 60
            )
            assert attrs.heartbeat_timeout.ToTimedelta() == timedelta(seconds=10)
            assert attrs.retry_policy.maximum_attempts == 1
        before = await state(runner_database, session.id)
        await Replayer(workflows=[RunnerWorkflow], plugins=[PydanticAIPlugin()]).replay_workflow(
            history
        )
        assert await state(runner_database, session.id) == before
        deleted = []
        original = runtime.delete_job

        async def interrupted_close(remote, job_id):
            if deleted:
                raise RuntimeError("one cleanup failure")
            await original(remote, job_id)
            deleted.append(job_id)

        monkeypatch.setattr(runtime, "delete_job", interrupted_close)
        with pytest.raises(RuntimeError, match="cleanup failure"):
            await service.close_session(session.id)
        assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING
        remaining = (await state(runner_database, session.id))[0]["jobs"]
        assert len(remaining) == 1 and deleted[0] not in remaining
        monkeypatch.setattr(runtime, "delete_job", original)
        assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
        assert (await state(runner_database, session.id))[0] == {
            "scope_id": str(session.id),
            "jobs": {},
        }
        for job_id in (job_a, job_b):
            with pytest.raises(ShellctlClientError) as caught:
                await client.status(job_id)
            assert caught.value.code == "job_not_found"


async def test_model_failure_preserves_previous_successful_snapshot(runner_database, shell_server):
    async with worker_service(runner_database) as service:
        session = await create_shell_session(runner_database, service, shell_server)
        first = await service.start_durable_runner(
            session.id, user_prompt=prompt((run("printf 'first\\n'; sleep 30"),))
        )
        assert "first" in await first.result()
        before = await state(runner_database, session.id)
        handle = await service.start_durable_runner(
            session.id, user_prompt=prompt((run("printf 'saved\\n'; sleep 30"),), fail=True)
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()
        assert await state(runner_database, session.id) == before
        assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED


@pytest.mark.parametrize("signal", [ModelRetry, UserError])
async def test_tool_signal_does_not_return_local_state(
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
        handle = await service.start_durable_runner(
            session.id, user_prompt=prompt((run("printf 'retry\\n'; sleep 30"),))
        )
        if signal is ModelRetry:
            assert "retry" in await handle.result()
        else:
            with pytest.raises(WorkflowFailureError):
                await handle.result()
        saved, version = await state(runner_database, session.id)
        assert saved["scope_id"] == str(session.id)
        if signal is ModelRetry:
            assert list(saved["jobs"]) == jobs[1:] and len(jobs) == 2 and version == 1
            assert saved["jobs"][jobs[1]] > 0
        else:
            assert saved["jobs"] == {} and len(jobs) == 1 and version == 0
        # State failure does not roll back the already-created remote job.
        assert (await client.status(jobs[0])).job_id == jobs[0]
        await service.close_session(session.id)


async def test_cancel_after_job_registration_keeps_previous_snapshot(
    runner_database, shell_server, monkeypatch
):
    registered = asyncio.Event()
    cleanup_started, release_cleanup, cleanup_finished = (
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )
    closing_client: ShellctlClient | None = None
    original_close = ShellctlClient.close

    async def blocked_result(self, result):
        nonlocal closing_client
        assert activity.in_activity()
        closing_client = self.client
        registered.set()
        await asyncio.Future()

    async def delayed_close(client: ShellctlClient) -> None:
        # Gate only this Activity's real client; fixture and scope clients still close normally.
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
        handle = await service.start_durable_runner(
            session.id, user_prompt=prompt((run("printf 'cancel\\n'; sleep 30"),))
        )
        result = asyncio.create_task(handle.result())
        try:
            await asyncio.wait_for(registered.wait(), 10)
            await handle.cancel()
            await asyncio.wait_for(cleanup_started.wait(), 10)
            # Cancellation must wait for resource cleanup before completing the Workflow.
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(result), 0.5)
            release_cleanup.set()
            with pytest.raises(WorkflowFailureError):
                await asyncio.wait_for(result, 20)
            await asyncio.wait_for(cleanup_finished.wait(), 5)
            saved, version = await state(runner_database, session.id)
            assert saved == {"scope_id": str(session.id), "jobs": {}} and version == 0
        finally:
            release_cleanup.set()
            if not result.done():
                await handle.cancel()
            await asyncio.wait_for(asyncio.gather(result, return_exceptions=True), 20)
            await service.close_session(session.id)

"""Explicit resume over real standalone Activities, PostgreSQL and SDK resources."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import Hooks
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from runner import test_activity
from temporalio.client import ActivityFailureError
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from kapy.agent_history import HistoryMessage, HistoryRepository
from kapy.application.resources import connect_temporal
from kapy.control.models import ModelService
from kapy.control.models.models import ModelRow, ProviderRow
from kapy.control.sessions import SessionBusy, SessionService, UpdateSession
from kapy.control.sessions.models import SessionRow
from kapy.control.sessions.repository import SessionRepository
from kapy.interfaces.http import create_router
from kapy.lifecycle import LifecycleStatus
from kapy.runner import RunnerActivity, RunnerDeps, RunnerInput, activities
from kapy.runner.checkpoints import (
    Checkpoint,
    DoneNext,
    ModelRequestNext,
    decode_state,
    validate_history,
)
from kapy.runner.recording import Recorder

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
endpoint = test_activity.endpoint
make_config = test_activity.make_config
start = test_activity.start


@asynccontextmanager
async def runtime(database):
    client = await connect_temporal(database.settings)
    queue = f"checkpoint-resume-{uuid4()}"
    runner = RunnerActivity(database.sessions, database.outputs, client)
    service = SessionService(
        database.sessions,
        output_service=database.outputs,
        temporal_client=client,
        temporal_task_queue=queue,
    )
    async with Worker(
        client,
        task_queue=queue,
        activities=[runner.create, runner.run, runner.resume, runner.close],
    ):
        yield service, client, queue


def lose_acknowledgment(monkeypatch, target, *, next_kind=None):
    original = Recorder.persist_messages
    stopped = []

    async def persist(self, deps, messages, *, candidate, **kwargs):
        accepted = await original(self, deps, messages, candidate=candidate, **kwargs)
        if (
            not stopped
            and candidate.checkpoint == target
            and (next_kind is None or candidate.next.kind == next_kind)
        ):
            stopped.append(deepcopy(candidate))
            raise RuntimeError("commit reply was lost")
        return accepted

    monkeypatch.setattr(Recorder, "persist_messages", persist)
    return stopped


async def saved(database):
    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, database.session_id)
        state = decode_state(row.runner_state)
        assert isinstance(state, Checkpoint)
        validate_history(state, await HistoryRepository(db).read_history_entries(row.id))
        return state, row.layer_states, row.status, row.current_activity_id


@pytest.mark.parametrize(
    "boundary",
    ["before_model_request", "model_node_completed", "tools_node_completed", "run_completed"],
)
async def test_uncertain_commit_resumes_latest_boundary_once(
    endpoint, runner_database, monkeypatch, boundary
):
    base_url, requests = endpoint
    hook_calls, resource_exits = [], []

    async def after_run(ctx, *, result):
        hook_calls.append(ctx.deps.activity_id)
        result.all_messages().append(ModelRequest([UserPromptPart("after-run tail")]))
        return replace(result, output="postprocessed result")

    original_provider = activities.build_provider

    @asynccontextmanager
    async def provider(*args, **kwargs):
        try:
            async with original_provider(*args, **kwargs) as value:
                yield value
        finally:
            resource_exits.append("provider")

    monkeypatch.setattr(activities, "build_provider", provider)
    monkeypatch.setattr(
        activities,
        "agent",
        Agent(
            deps_type=RunnerDeps,
            instructions="Process the saved answer before asking for another model response.",
            capabilities=[
                *activities.agent.root_capability.capabilities,
                Hooks(after_run=after_run),
            ],
        ),
    )
    stopped = lose_acknowledgment(monkeypatch, boundary)
    async with runtime(runner_database) as (service, client, queue):
        first = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="original input",
                config=make_config(base_url),
            ),
            database=runner_database,
        )
        with pytest.raises(ActivityFailureError):
            await first.result()
        state, _, status, owner = await saved(runner_database)
        assert state.model_dump(mode="json") == stopped[0].model_dump(mode="json")
        assert status == LifecycleStatus.READY and owner is None
        assert len(requests) == (0 if boundary == "before_model_request" else 1)
        if boundary != "run_completed":
            with pytest.raises(SessionBusy):
                await service.update_session(
                    runner_database.session_id, UpdateSession(title="cannot edit unfinished turn")
                )
            competing = await service.start_runner(
                runner_database.session_id, activity_id=uuid4(), user_prompt="new input"
            )
            with pytest.raises(ActivityFailureError):
                await competing.result()
        else:
            # Done must work without even parsing deleted model/provider or layers.
            async with runner_database.sessions.begin() as db:
                row = await db.get(SessionRow, runner_database.session_id)
                await db.delete(await db.get(ModelRow, (row.provider_id, row.model_name)))
                await db.delete(await db.get(ProviderRow, row.provider_id))
                row.layers, row.layer_states = {"uninstalled": {"invalid": True}}, {}
        resumed_id = uuid4()
        resumed = await service.resume_runner(runner_database.session_id, activity_id=resumed_id)
        assert await resumed.result() == "postprocessed result"
        # Same native key retrieves its retained result, with no new execution.
        same = await service.resume_runner(runner_database.session_id, activity_id=resumed_id)
        assert same.run_id == resumed.run_id and await same.result() == "postprocessed result"
        current = await service.get_activity(
            runner_database.session_id, resumed_id, run_id=resumed.run_id
        )
        assert current.activity_type == "kapy.resume_agent"
        assert current.status == "COMPLETED" and current.result == "postprocessed result"
        original = await service.get_activity(
            runner_database.session_id, UUID(first.id), run_id=first.run_id
        )
        assert original.status == "FAILED" and original.result is None
    assert len(requests) == 1 and len(hook_calls) == 1
    assert len(resource_exits) == (1 if boundary == "run_completed" else 2)
    state, _, status, owner = await saved(runner_database)
    assert state.next == DoneNext(output="postprocessed result")
    assert status == LifecycleStatus.READY and owner is None
    assert [(m.metadata or {})["seq"] for m in state.messages] == [0, 1, 2]
    assert (
        sum(
            isinstance(p, UserPromptPart) and p.content == "original input"
            for m in state.messages
            for p in m.parts
        )
        == 1
    )


@pytest.mark.parametrize("boundary", ["model_node_completed", "tools_node_completed"])
async def test_saved_tool_calls_resume_once_and_committed_receipts_are_not_replayed(
    endpoint, runner_database, monkeypatch, boundary
):
    base_url, requests = endpoint
    calls, loaded, model_requests = [], [], []

    class Model(TestModel):
        async def request(self, messages, *args, **kwargs):
            model_requests.append(deepcopy(messages))
            return await super().request(messages, *args, **kwargs)

        @asynccontextmanager
        async def request_stream(self, messages, *args, **kwargs):
            model_requests.append(deepcopy(messages))
            async with super().request_stream(messages, *args, **kwargs) as response:
                yield response

    def work(ctx: RunContext[RunnerDeps]) -> str:
        calls.append(ctx.deps.activity_id)
        jobs = ctx.deps.layers["shell"]["state"]["jobs"]
        assert isinstance(jobs, dict)
        jobs["saved-job"] = 17
        return "tool result"

    async def before_run(ctx):
        loaded.append(deepcopy(ctx.deps.layers["shell"]["state"]))

    @asynccontextmanager
    async def model(*args, **kwargs):
        yield Model(call_tools=["work"], custom_output_text="done")

    monkeypatch.setattr(activities, "build_model", model)
    monkeypatch.setattr(
        activities,
        "agent",
        Agent(
            deps_type=RunnerDeps,
            tools=[work],
            capabilities=[
                *activities.agent.root_capability.capabilities,
                Hooks(before_run=before_run),
            ],
        ),
    )
    stopped = lose_acknowledgment(monkeypatch, boundary)
    async with runtime(runner_database) as (service, client, queue):
        first = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="run work",
                config=make_config(base_url),
                layers={
                    "shell": {
                        "config": {"cwd": "/tmp"},
                        "state": {"scope_id": str(runner_database.session_id), "jobs": {}},
                    }
                },
            ),
            database=runner_database,
        )
        with pytest.raises(ActivityFailureError):
            await first.result()
        state, states, _, _ = await saved(runner_database)
        expected_jobs = {} if boundary == "model_node_completed" else {"saved-job": 17}
        assert state == stopped[0] and states["shell"]["jobs"] == expected_jobs
        assert len(calls) == (0 if boundary == "model_node_completed" else 1)
        assert len(model_requests) == 1
        assert isinstance(state.messages[1].parts[0], ToolCallPart)
        resumed = await service.resume_runner(runner_database.session_id, activity_id=uuid4())
        assert await resumed.result() == "done"
    assert requests == [] and len(calls) == 1
    assert loaded[0]["jobs"] == {} and loaded[1]["jobs"] == expected_jobs
    assert len(model_requests) == 2
    assert model_requests[1][1] == stopped[0].messages[1]
    receipt = model_requests[1][2]
    assert isinstance(receipt.parts[0], ToolReturnPart)
    assert receipt.parts[0].tool_name == "work" and receipt.parts[0].content == "tool result"
    assert receipt.parts[0].tool_call_id == stopped[0].messages[1].parts[0].tool_call_id
    state, states, _, _ = await saved(runner_database)
    assert states["shell"]["jobs"] == {"saved-job": 17}
    assert [(m.metadata or {})["seq"] for m in state.messages] == [0, 1, 2, 3]
    assert (
        state.messages[2].parts == receipt.parts and state.messages[2].metadata == receipt.metadata
    )
    assert state.messages[1].run_id != state.messages[3].run_id


@pytest.mark.parametrize("old_owner", ["terminal", "missing"])
async def test_resume_cas_takes_only_a_confirmed_stale_exact_execution(
    endpoint, runner_database, monkeypatch, old_owner
):
    base_url, requests = endpoint
    lose_acknowledgment(monkeypatch, "before_model_request")
    async with runtime(runner_database) as (service, client, queue):
        first = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="go",
                config=make_config(base_url),
            ),
            database=runner_database,
        )
        with pytest.raises(ActivityFailureError):
            await first.result()
        old_id = UUID(first.id) if old_owner == "terminal" else uuid4()
        old_run = first.run_id if old_owner == "terminal" else str(uuid4())
        async with runner_database.sessions.begin() as db:
            row = await db.get(SessionRow, runner_database.session_id)
            row.current_activity_id, row.current_activity_run_id = old_id, old_run
        resumed = await service.resume_runner(runner_database.session_id, activity_id=uuid4())
        assert await resumed.result() == "done"
    assert len(requests) == 1


async def test_resume_keeps_a_running_writer_and_uses_http_activity_reference(
    endpoint, runner_database, monkeypatch
):
    from pydantic_ai.models.openai import OpenAIChatModel

    base_url, requests = endpoint
    entered, release = asyncio.Event(), asyncio.Event()
    original = OpenAIChatModel.request_stream

    @asynccontextmanager
    async def blocked(self, *args, **kwargs):
        entered.set()
        await release.wait()
        async with original(self, *args, **kwargs) as response:
            yield response

    monkeypatch.setattr(OpenAIChatModel, "request_stream", blocked)
    async with runtime(runner_database) as (service, client, queue):
        first = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="go",
                config=make_config(base_url),
            ),
            database=runner_database,
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            app = FastAPI()
            app.include_router(create_router(ModelService(runner_database.sessions), service))
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as http:
                body = {"activity_id": str(uuid4())}
                response = await http.post(
                    f"/api/sessions/{runner_database.session_id}/runner/resume", json=body
                )
                assert response.status_code == 202
                reference = response.json()
                rejected = client.get_activity_handle(
                    reference["activity_id"], run_id=reference["run_id"], result_type=str
                )
                with pytest.raises(ActivityFailureError):
                    await rejected.result()
                row = await service.get_session(runner_database.session_id)
                assert (row.current_activity_id, row.current_activity_run_id) == (
                    UUID(first.id),
                    first.run_id,
                )
                invalid = await http.post(
                    f"/api/sessions/{runner_database.session_id}/runner/resume",
                    json=body | {"user_prompt": "forbidden"},
                )
                assert invalid.status_code == 422
        finally:
            release.set()
            assert await first.result() == "done"
    assert len(requests) == 1


@pytest.mark.parametrize(
    "status", [LifecycleStatus.CREATING, LifecycleStatus.CLOSING, LifecycleStatus.CLOSED]
)
async def test_closed_or_uninitialized_scopes_never_resume(runner_database, status):
    deps = runner_database.deps()
    await deps.recorder.record_messages(
        deps,
        [ModelRequest([UserPromptPart("saved input")])],
        checkpoint="before_model_request",
        next=ModelRequestNext(),
    )
    before, states, _, _ = await saved(runner_database)
    async with runner_database.sessions.begin() as db:
        row = await db.get(SessionRow, runner_database.session_id)
        row.status, row.current_activity_id, row.current_activity_run_id = status, None, None
    async with runtime(runner_database) as (service, client, queue):
        resumed = await service.resume_runner(runner_database.session_id, activity_id=uuid4())
        with pytest.raises(ActivityFailureError) as failure:
            await resumed.result()
        assert isinstance(failure.value.cause, ApplicationError)
        assert failure.value.cause.type == "LifecycleError"
        row = await service.get_session(runner_database.session_id)
        assert row.status == status and row.current_activity_id is None
        after, after_states, _, _ = await saved(runner_database)
        assert after == before and after_states == states


async def test_queued_resume_reads_the_latest_done_at_admission(runner_database):
    deps = runner_database.deps()
    await deps.recorder.record_messages(
        deps, [], checkpoint="run_completed", next=DoneNext(output="old"), release=True
    )
    client = await connect_temporal(runner_database.settings)
    queue = f"queued-resume-{uuid4()}"
    service = SessionService(
        runner_database.sessions, temporal_client=client, temporal_task_queue=queue
    )
    handle = await service.resume_runner(runner_database.session_id, activity_id=uuid4())
    key, run_id = uuid4(), str(uuid4())
    async with runner_database.sessions.begin() as db:
        await SessionRepository(db).claim_run(deps.session_id, key, run_id)
    replacement = replace(deps, activity_id=key, activity_run_id=run_id, recorder=Recorder())
    await replacement.recorder.record_messages(
        replacement, [], checkpoint="run_completed", next=DoneNext(output="latest"), release=True
    )
    runner = RunnerActivity(runner_database.sessions, runner_database.outputs, client)
    async with Worker(client, task_queue=queue, activities=[runner.resume]):
        assert await handle.result() == "latest"


async def test_resource_exit_failure_keeps_finish_and_resume_repeats_only_after_run(
    endpoint, runner_database, monkeypatch
):
    base_url, requests = endpoint
    hooks, exits = [], []
    original = activities.build_provider

    async def tail(ctx, *, result):
        hooks.append(ctx.deps.activity_id)
        result.all_messages().append(ModelRequest([UserPromptPart("final tail")]))
        return result

    @asynccontextmanager
    async def provider(*args, **kwargs):
        async with original(*args, **kwargs) as value:
            yield value
        exits.append(True)
        if len(exits) == 1:
            raise RuntimeError("provider exit failed")

    monkeypatch.setattr(activities, "build_provider", provider)
    monkeypatch.setattr(
        activities,
        "agent",
        Agent(
            deps_type=RunnerDeps,
            capabilities=[*activities.agent.root_capability.capabilities, Hooks(after_run=tail)],
        ),
    )
    async with runtime(runner_database) as (service, client, queue):
        first = await start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="go",
                config=make_config(base_url),
            ),
            database=runner_database,
        )
        with pytest.raises(ActivityFailureError):
            await first.result()
        state, _, status, owner = await saved(runner_database)
        assert state.next.kind == "finish" and len(state.messages) == 2
        assert status == LifecycleStatus.READY and owner is None
        resumed = await service.resume_runner(runner_database.session_id, activity_id=uuid4())
        assert await resumed.result() == "done"
    assert len(requests) == 1 and len(hooks) == len(exits) == 2
    state, _, _, _ = await saved(runner_database)
    assert isinstance(state.next, DoneNext) and len(state.messages) == 3


@pytest.mark.parametrize("legacy", ["valid_tail", "ahead", "null", "interior"])
async def test_legacy_start_preserves_gaps_and_rejects_inconsistent_history(
    endpoint, runner_database, legacy
):
    base_url, requests = endpoint
    session_id = runner_database.session_id
    prefix = [
        ModelRequest([UserPromptPart("old input")], metadata={"seq": 5}),
        ModelResponse([TextPart("old answer")], metadata={"seq": 10}),
    ]
    tail = ModelRequest([UserPromptPart("old after-run tail")])
    messages = (
        [prefix[0], tail, prefix[1]]
        if legacy == "interior"
        else prefix[:1]
        if legacy == "ahead"
        else [*prefix, tail]
    )
    raw = None if legacy == "null" else ModelMessagesTypeAdapter.dump_json(messages).decode()
    async with runner_database.sessions.begin() as db:
        await SessionRepository(db).guard(
            session_id, session_id, "test-run", (LifecycleStatus.READY,)
        )
        await HistoryRepository(db).append_history(
            session_id,
            [HistoryMessage(session_id, seq, m) for seq, m in zip([5, 10], prefix, strict=True)],
        )
    async with runtime(runner_database) as (service, client, queue):
        first = await start(
            client,
            queue,
            RunnerInput(
                session_id=session_id,
                runner_state=raw,
                user_prompt="new input",
                config=make_config(base_url),
            ),
            database=runner_database,
        )
        if legacy == "valid_tail":
            assert await first.result() == "done"
            state, _, _, _ = await saved(runner_database)
            assert [(m.metadata or {})["seq"] for m in state.messages] == [5, 10, 11, 12, 13]
            assert len(requests) == 1
        else:
            with pytest.raises(ActivityFailureError):
                await first.result()
            assert requests == []
            async with runner_database.sessions.begin() as db:
                assert (await db.get(SessionRow, session_id)).runner_state == raw
        if legacy != "valid_tail":
            resumed = await service.resume_runner(session_id, activity_id=uuid4())
            with pytest.raises(ActivityFailureError):
                await resumed.result()
            assert (await service.get_session(session_id)).status == LifecycleStatus.READY

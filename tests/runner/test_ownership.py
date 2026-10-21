"""Actual callback fencing over SDK dispatch, native Activities and PostgreSQL."""

import asyncio
from contextlib import asynccontextmanager
from threading import get_ident
from uuid import UUID, uuid4

import pytest
from pydantic_ai import Agent, ModelRetry, RunContext, TextOutput, ToolOutput
from pydantic_ai.capabilities import (
    AbstractCapability,
    CapabilityOrdering,
    CombinedCapability,
    Hooks,
    on_event,
)
from pydantic_ai.messages import ModelRequest, PartStartEvent, TextPart, UserPromptPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.run import AgentRunResult
from pydantic_ai.usage import RunUsage
from runner import test_activity
from runner.test_resume import lose_acknowledgment, runtime, saved
from temporalio.client import ActivityFailureError
from temporalio.exceptions import ApplicationError, CancelledError

from kapy.control.sessions.repository import SessionRepository
from kapy.control.sessions.types import OwnershipLost
from kapy.lifecycle import LifecycleStatus
from kapy.runner import RunnerDeps, RunnerInput, activities
from kapy.runner.checkpoints import ModelRequestNext
from kapy.runner.ownership import fence
from kapy.runner.recording import Recorder

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]
endpoint = test_activity.endpoint


@pytest.mark.parametrize("takeover", [False, True])
async def test_output_validators_keep_native_retry_and_stop_after_takeover(
    endpoint, runner_database, monkeypatch, takeover
):
    base_url, requests = endpoint
    entered, release = asyncio.Event(), asyncio.Event()
    calls, loop_thread = [], get_ident()
    agent = Agent(
        deps_type=RunnerDeps,
        capabilities=activities.agent.root_capability.capabilities,
    )

    @agent.output_validator
    async def first(ctx: RunContext[RunnerDeps], output: str) -> str:
        assert ctx.deps.session_id == runner_database.session_id
        calls.append(("first", output))
        entered.set()
        await asyncio.wait_for(release.wait(), 15)
        if not takeover and len(calls) == 1:
            raise ModelRetry("try the output again")
        return output + "-first"

    @agent.output_validator
    def second(output: str) -> str:
        assert get_ident() != loop_thread
        calls.append(("second", output))
        return output + "-second"

    @agent.output_validator
    def third(ctx: RunContext[RunnerDeps], output: str) -> str:
        assert ctx.deps.session_id == runner_database.session_id
        calls.append(("third", output))
        return output + "-third"

    @agent.output_validator
    async def fourth(output: str) -> str:
        calls.append(("fourth", output))
        return output + "-fourth"

    monkeypatch.setattr(activities, "agent", agent)
    new_owner, new_run = uuid4(), str(uuid4())
    async with runtime(runner_database) as (service, client, queue):
        handle = await test_activity.start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="go",
                config=test_activity.make_config(base_url),
            ),
            database=runner_database,
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            if takeover:
                async with runner_database.sessions.begin() as db:
                    assert (
                        await SessionRepository(db).claim_close_or_run(
                            runner_database.session_id,
                            new_owner,
                            new_run,
                            close=False,
                            previous=(UUID(handle.id), handle.run_id, LifecycleStatus.READY),
                        )
                        is not None
                    )
        finally:
            release.set()
        if takeover:
            with pytest.raises(ActivityFailureError) as failure:
                await asyncio.wait_for(handle.result(), 30)
            assert isinstance(failure.value.cause, ApplicationError)
            assert failure.value.cause.type == "OwnershipLost"
            row = await service.get_session(runner_database.session_id)
            assert (row.current_activity_id, row.current_activity_run_id) == (new_owner, new_run)
        else:
            assert await asyncio.wait_for(handle.result(), 30) == "done-first-second-third-fourth"
    state, _, _, _ = await saved(runner_database)
    if takeover:
        assert calls == [("first", "done")] and len(requests) == 1
        assert state.next.kind == "call_tools"
    else:
        assert calls == [
            ("first", "done"),
            ("first", "done"),
            ("second", "done-first"),
            ("third", "done-first-second"),
            ("fourth", "done-first-second-third"),
        ]
        assert len(requests) == 2
        assert state.next.kind == "done" and state.next.output == "done-first-second-third-fourth"


@pytest.mark.parametrize("output_mode", ["text", "tool"])
@pytest.mark.parametrize("takeover", [False, True])
async def test_output_function_is_fenced_after_awaited_preparation(
    endpoint, runner_database, monkeypatch, output_mode, takeover
):
    base_url, requests = endpoint
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []
    previous_state, previous_layers = None, None

    async def prepare(ctx, *, output, output_context):
        entered.set()
        await asyncio.wait_for(release.wait(), 15)
        return output

    async def complete(ctx: RunContext[RunnerDeps], text: str) -> str:
        calls.append((ctx.deps.session_id, text))
        return text + " processed"

    @asynccontextmanager
    async def model(*args, **kwargs):
        try:
            yield (
                TestModel(custom_output_text="candidate")
                if output_mode == "text"
                else TestModel(custom_output_args={"text": "candidate"})
            )
        finally:
            closed.set()

    monkeypatch.setattr(activities, "build_model", model)
    monkeypatch.setattr(
        activities,
        "agent",
        Agent(
            deps_type=RunnerDeps,
            output_type=TextOutput(complete) if output_mode == "text" else ToolOutput(complete),
            capabilities=[
                *activities.agent.root_capability.capabilities,
                Hooks(
                    before_output_process=prepare,
                    ordering=CapabilityOrdering(position="innermost"),
                ),
            ],
        ),
    )
    new_owner, new_run = uuid4(), str(uuid4())
    async with runtime(runner_database) as (service, client, queue):
        handle = await test_activity.start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="go",
                config=test_activity.make_config(base_url),
            ),
            database=runner_database,
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            if takeover:
                previous_state, previous_layers, _, _ = await saved(runner_database)
                async with runner_database.sessions.begin() as db:
                    assert (
                        await SessionRepository(db).claim_close_or_run(
                            runner_database.session_id,
                            new_owner,
                            new_run,
                            close=False,
                            previous=(UUID(handle.id), handle.run_id, LifecycleStatus.READY),
                        )
                        is not None
                    )
        finally:
            release.set()
        if takeover:
            with pytest.raises(ActivityFailureError) as failure:
                await asyncio.wait_for(handle.result(), 30)
            assert isinstance(failure.value.cause, ApplicationError)
            assert failure.value.cause.type == "OwnershipLost"
            row = await service.get_session(runner_database.session_id)
            assert (row.current_activity_id, row.current_activity_run_id) == (new_owner, new_run)
        else:
            assert await asyncio.wait_for(handle.result(), 30) == "candidate processed"
    state, layers, status, owner = await saved(runner_database)
    assert closed.is_set() and requests == [] and status == LifecycleStatus.READY
    if takeover:
        assert calls == [] and owner == new_owner
        assert state == previous_state and layers == previous_layers
        assert state.checkpoint == "model_node_completed" and state.next.kind == "call_tools"
    else:
        assert calls == [(runner_database.session_id, "candidate")] and owner is None
        assert state.next.kind == "done" and state.next.output == "candidate processed"


@pytest.mark.parametrize("interruption", ["takeover", "cancel"])
async def test_interrupted_hook_cannot_dispatch_later_hooks(
    endpoint, runner_database, monkeypatch, interruption
):
    base_url, requests = endpoint
    entered, release, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancelled = asyncio.Event()
    calls = []

    async def first(ctx, *, result):
        calls.append("first")
        entered.set()
        try:
            await asyncio.wait_for(release.wait(), 15)
        except asyncio.CancelledError:
            if interruption != "cancel":
                raise
            cancelled.set()
        finally:
            calls.append("hook finally")
        return result

    async def second(ctx, *, result):
        calls.append("second")
        return result

    hooks = Hooks(after_run=first)
    hooks.on.after_run(second)
    monkeypatch.setattr(
        activities,
        "agent",
        Agent(
            deps_type=RunnerDeps,
            capabilities=[*activities.agent.root_capability.capabilities, hooks],
        ),
    )
    original = activities.build_provider

    @asynccontextmanager
    async def provider(*args, **kwargs):
        try:
            async with original(*args, **kwargs) as value:
                yield value
        finally:
            closed.set()

    monkeypatch.setattr(activities, "build_provider", provider)
    new_owner, new_run = uuid4(), str(uuid4())
    async with runtime(runner_database) as (service, client, queue):
        handle = await test_activity.start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="go",
                config=test_activity.make_config(base_url),
            ),
            database=runner_database,
        )
        try:
            await asyncio.wait_for(entered.wait(), 10)
            if interruption == "cancel":
                await asyncio.wait_for(handle.cancel(), 10)
                await asyncio.wait_for(cancelled.wait(), 10)
            else:
                async with runner_database.sessions.begin() as db:
                    assert (
                        await SessionRepository(db).claim_close_or_run(
                            runner_database.session_id,
                            new_owner,
                            new_run,
                            close=False,
                            previous=(UUID(handle.id), handle.run_id, LifecycleStatus.READY),
                        )
                        is not None
                    )
        finally:
            release.set()
        with pytest.raises(ActivityFailureError) as failure:
            await asyncio.wait_for(handle.result(), 30)
        if interruption == "cancel":
            assert isinstance(failure.value.cause, CancelledError)
        else:
            assert isinstance(failure.value.cause, ApplicationError)
            assert failure.value.cause.type == "OwnershipLost"
        row = await service.get_session(runner_database.session_id)
        expected = (None, None) if interruption == "cancel" else (new_owner, new_run)
        assert (row.current_activity_id, row.current_activity_run_id) == expected
    assert calls == ["first", "hook finally"] and closed.is_set()
    assert len(requests) == 1
    state, _, _, _ = await saved(runner_database)
    assert state.next.kind == "finish"


@pytest.mark.parametrize("takeover", [False, True])
async def test_sdk_error_chain_only_enters_owned_recovery_callbacks(runner_database, takeover):
    deps, calls = runner_database.deps(), []
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    async def decline(ctx, *, error):
        calls.append("decline")
        if takeover:
            async with runner_database.sessions.begin() as db:
                assert (
                    await SessionRepository(db).claim_close_or_run(
                        deps.session_id,
                        uuid4(),
                        str(uuid4()),
                        close=False,
                        previous=(deps.activity_id, deps.activity_run_id, LifecycleStatus.READY),
                    )
                    is not None
                )
        raise RuntimeError("try the next business handler")

    async def recover(ctx, *, error):
        calls.append("recover")
        return AgentRunResult(output="recovered")

    class Fallback(AbstractCapability[RunnerDeps]):
        async def on_run_error(self, ctx, *, error):
            calls.append("fallback")
            return AgentRunResult(output="fallback")

    hooks = Hooks(run_error=decline)
    hooks.on.run_error(recover)
    callbacks = fence(CombinedCapability([Fallback(), hooks]))
    if takeover:
        with pytest.raises(OwnershipLost):
            await callbacks.on_run_error(ctx, error=ValueError("business error"))
        assert calls == ["decline"]
    else:
        result = await callbacks.on_run_error(ctx, error=ValueError("business error"))
        assert result.output == "recovered" and calls == ["decline", "recover"]


async def test_uncertain_save_cannot_enter_business_error_recovery(
    endpoint, runner_database, monkeypatch
):
    base_url, requests = endpoint
    calls = []

    async def recover(ctx, *, error, **kwargs):
        calls.append("recovery")
        raise error

    hooks = Hooks(run_error=recover, node_run_error=recover, model_request_error=recover)
    hooks.on.run_error(recover)
    monkeypatch.setattr(
        activities,
        "agent",
        Agent(
            deps_type=RunnerDeps,
            capabilities=[*activities.agent.root_capability.capabilities, hooks],
        ),
    )
    stopped = lose_acknowledgment(monkeypatch, "model_node_completed")
    async with runtime(runner_database) as (service, client, queue):
        handle = await test_activity.start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="go",
                config=test_activity.make_config(base_url),
            ),
            database=runner_database,
        )
        with pytest.raises(ActivityFailureError):
            await asyncio.wait_for(handle.result(), 30)
    state, _, status, owner = await saved(runner_database)
    assert state.model_dump(mode="json") == stopped[0].model_dump(mode="json")
    assert state.next.kind == "call_tools"
    assert calls == [] and len(requests) == 1
    assert status == LifecycleStatus.READY and owner is None


@pytest.mark.parametrize("registration", ["for_run", "marked_event"])
async def test_nested_callback_dispatch_checks_each_new_entry(runner_database, registration):
    deps, calls = runner_database.deps(), []
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())

    async def switch_writer():
        async with runner_database.sessions.begin() as db:
            assert (
                await SessionRepository(db).claim_close_or_run(
                    deps.session_id,
                    uuid4(),
                    str(uuid4()),
                    close=False,
                    previous=(deps.activity_id, deps.activity_run_id, LifecycleStatus.READY),
                )
                is not None
            )

    async def first(ctx, *, result):
        calls.append("first")
        await switch_writer()
        return result

    async def second(ctx, *, result):
        calls.append("second")
        return result

    class Registered(Hooks[RunnerDeps]):
        async def for_run(self, ctx):
            self.on.after_run(first)
            self.on.after_run(second)
            return self

    class Marked(AbstractCapability[RunnerDeps]):
        @on_event
        async def first(self, ctx, event):
            calls.append("first")
            await switch_writer()

        @on_event
        async def second(self, ctx, event):
            calls.append("second")

    if registration == "for_run":
        callbacks = await fence(Registered()).for_run(ctx)
        with pytest.raises(OwnershipLost):
            await callbacks.after_run(ctx, result=AgentRunResult(output="done"))
    else:
        with pytest.raises(OwnershipLost):
            await fence(Marked()).on_event(ctx, event=PartStartEvent(index=0, part=TextPart("x")))
    assert calls == ["first"]


async def test_callback_rechecks_stopped_save_after_waiting_for_guard(runner_database, monkeypatch):
    deps, calls = runner_database.deps(), []
    ctx = RunContext(deps=deps, model=TestModel(), usage=RunUsage())
    entered, task = asyncio.Event(), None
    guard = SessionRepository.guard

    async def after_run(ctx, *, result):
        calls.append("callback")
        return result

    async def waiting_guard(self, *args, **kwargs):
        entered.set()
        return await guard(self, *args, **kwargs)

    try:
        async with runner_database.sessions.begin() as db:
            await guard(
                SessionRepository(db),
                deps.session_id,
                deps.activity_id,
                deps.activity_run_id,
                (LifecycleStatus.READY,),
            )
            monkeypatch.setattr(SessionRepository, "guard", waiting_guard)
            callbacks = fence(Hooks(after_run=after_run))
            task = asyncio.create_task(
                callbacks.after_run(ctx, result=AgentRunResult(output="done"))
            )
            await asyncio.wait_for(entered.wait(), 10)
            # A failed save stops the same execution while its callback waits
            # for the row. The guard will still find the matching full writer.
            with pytest.raises(ValueError):
                await deps.recorder.record_messages(
                    deps,
                    [ModelRequest([UserPromptPart("invalid position")], metadata={"seq": 0})],
                    checkpoint="before_model_request",
                    next=ModelRequestNext(),
                )
        with pytest.raises(ValueError):
            await asyncio.wait_for(task, 10)
        assert calls == []
    finally:
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def test_model_is_fenced_after_an_awaited_initial_save(
    endpoint, runner_database, monkeypatch
):
    base_url, requests = endpoint
    persist = Recorder.persist_messages
    replacement, replacement_run = uuid4(), str(uuid4())

    async def switch_after_commit(self, deps, *args, **kwargs):
        accepted = await persist(self, deps, *args, **kwargs)
        async with runner_database.sessions.begin() as db:
            assert (
                await SessionRepository(db).claim_close_or_run(
                    deps.session_id,
                    replacement,
                    replacement_run,
                    close=False,
                    previous=(deps.activity_id, deps.activity_run_id, LifecycleStatus.READY),
                )
                is not None
            )
        return accepted

    monkeypatch.setattr(Recorder, "persist_messages", switch_after_commit)
    async with runtime(runner_database) as (service, client, queue):
        handle = await test_activity.start(
            client,
            queue,
            RunnerInput(
                session_id=runner_database.session_id,
                runner_state=None,
                user_prompt="go",
                config=test_activity.make_config(base_url),
            ),
            database=runner_database,
        )
        with pytest.raises(ActivityFailureError) as failure:
            await asyncio.wait_for(handle.result(), 30)
        assert isinstance(failure.value.cause, ApplicationError)
        assert failure.value.cause.type == "OwnershipLost"
        row = await service.get_session(runner_database.session_id)
        assert (row.current_activity_id, row.current_activity_run_id) == (
            replacement,
            replacement_run,
        )
    state, _, _, _ = await saved(runner_database)
    assert state.checkpoint == "before_model_request" and requests == []

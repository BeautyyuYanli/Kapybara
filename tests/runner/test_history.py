"""Checkpoints follow actual SDK nodes; persistence alone is replaced in these tests."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic_ai import Agent, ModelRequestNode, ToolOutput, capture_run_messages
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering, Hooks
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy.ext.asyncio import async_sessionmaker

from kapy.agent_output import AgentOutputService
from kapy.runner import MessageRecordCapability, Recorder, RunnerDeps
from kapy.runner.checkpoints import (
    CallToolsNext,
    Checkpoint,
    DoneNext,
    FinishNext,
    ModelRequestNext,
)

pytestmark = pytest.mark.asyncio


def make_deps(history=(), *, resume=None):
    outputs = Mock(spec=AgentOutputService)

    @asynccontextmanager
    async def publisher(session_id):
        async def publish(event):
            pass

        yield publish

    outputs.publisher = publisher
    return RunnerDeps(
        session_id=uuid4(),
        activity_id=uuid4(),
        activity_run_id="test-run",
        session_factory=Mock(spec=async_sessionmaker),
        output_service=outputs,
        recorder=Recorder(history, resuming=resume is not None),
        resume=resume,
    )


async def run(agent, deps, prompt="go", history=(), **kwargs):
    with capture_run_messages() as messages:
        deps.messages = messages
        return await agent.run(prompt, deps=deps, message_history=deepcopy(list(history)), **kwargs)


async def before_request(ctx, request):
    return await ctx.deps.recorder.before_model_request(ctx, request)


def recording_agent(model, *capabilities, **kwargs):
    return Agent(
        model,
        deps_type=RunnerDeps,
        capabilities=[
            MessageRecordCapability(),
            *capabilities,
            Hooks(
                before_model_request=before_request,
                ordering=CapabilityOrdering(position="innermost"),
            ),
        ],
        **kwargs,
    )


@pytest.fixture
def recorded(monkeypatch):
    saves = []

    async def persist(self, deps, messages, *, candidate, layer_states, release=False):
        saves.append(
            SimpleNamespace(
                messages=deepcopy(messages),
                state=deepcopy(candidate),
                states=deepcopy(layer_states),
                release=release,
                session_id=deps.session_id,
            )
        )
        await asyncio.sleep(0)
        return tuple(messages)

    monkeypatch.setattr(Recorder, "persist_messages", persist)
    return saves


class FinishMessages(AbstractCapability[RunnerDeps]):
    async def before_model_request(self, ctx, request_context):
        request_context.messages[-1].parts.append(UserPromptPart("late input"))
        return replace(
            request_context,
            model_request_parameters=replace(
                request_context.model_request_parameters,
                instruction_parts=[InstructionPart("confirmed instructions")],
            ),
        )

    async def after_node_run(self, ctx, *, node, result):
        if isinstance(node, ModelRequestNode):
            ctx.messages[-1].parts = [TextPart("finished")]
        return result

    async def after_run(self, ctx, *, result):
        result.all_messages().append(ModelRequest([UserPromptPart("after-run tail")]))
        return result


async def test_initial_model_end_and_after_run_have_distinct_checkpoints(recorded):
    deps = make_deps()

    async def respond(messages, info):
        assert len(recorded) == 1
        assert recorded[0].state.checkpoint == "before_model_request"
        assert recorded[0].state.messages[-1].instructions == "confirmed instructions"
        assert messages[-1].metadata == {"seq": 0}
        assert deps.response_seq == 1
        return ModelResponse([TextPart("done")])

    result = await run(recording_agent(FunctionModel(respond), FinishMessages()), deps)
    assert [s.state.checkpoint for s in recorded] == [
        "before_model_request",
        "model_node_completed",
        "tools_node_completed",
    ]
    assert [[m.seq for m in s.messages] for s in recorded] == [[0], [1], []]
    assert recorded[-1].state.next == FinishNext(output="finished")
    assert result.all_messages()[-1].metadata is None
    await deps.recorder.record_messages(
        deps,
        result.all_messages(),
        checkpoint="run_completed",
        next=DoneNext(output=result.output),
        release=True,
    )
    assert recorded[-1].state.next == DoneNext(output="finished")
    assert [m.seq for m in recorded[-1].messages] == [2]
    assert recorded[-1].release
    assert [m.metadata["seq"] for m in result.all_messages()] == [0, 1, 2]


async def test_tool_result_request_is_saved_before_sdk_appends_it(recorded):
    calls = []

    def work() -> str:
        calls.append("work")
        return "ok"

    deps = make_deps()
    result = await run(recording_agent(TestModel(custom_output_text="done"), tools=[work]), deps)
    assert result.output == "done" and calls == ["work"]
    assert [[m.seq for m in s.messages] for s in recorded] == [[0], [1], [2], [3], []]
    tool_save = recorded[2].state
    assert tool_save.checkpoint == "tools_node_completed"
    assert tool_save.next == ModelRequestNext()
    assert tool_save.messages[-1].parts[0].part_kind == "tool-return"
    # Preparing the next send changes timestamp/instructions/SDK identity only in
    # the live view. Later snapshots keep the original frozen tool-result request.
    assert recorded[-1].state.messages[2] == tool_save.messages[2]
    assert len(result.all_messages()) == 4


async def test_model_failure_keeps_only_initial_request(recorded):
    async def fail(messages, info):
        raise RuntimeError("model failed")

    deps = make_deps()
    with pytest.raises(RuntimeError, match="model failed"):
        await run(recording_agent(FunctionModel(fail)), deps)
    assert [s.state.checkpoint for s in recorded] == ["before_model_request"]
    assert deps.recorder.boundary == 0


async def test_initial_save_failure_prevents_model_and_live_seq_acknowledgment(monkeypatch):
    calls = []
    saves = []

    async def respond(messages, info):
        calls.append(1)
        return ModelResponse([TextPart("done")])

    async def fail(*args, **kwargs):
        saves.append(kwargs)
        raise RuntimeError("commit unavailable")

    monkeypatch.setattr(Recorder, "persist_messages", fail)
    deps = make_deps()
    with pytest.raises(RuntimeError, match="commit unavailable") as failed:
        await run(recording_agent(FunctionModel(respond)), deps)
    assert calls == []
    assert deps.messages[0].metadata is None
    assert deps.recorder.boundary == -1
    with pytest.raises(RuntimeError, match="commit unavailable") as stopped:
        await deps.recorder.record_messages(
            deps, deps.messages, checkpoint="before_model_request", next=ModelRequestNext()
        )
    assert stopped.value is failed.value
    assert len(saves) == 1


@pytest.mark.parametrize("mutation", ["parts", "metadata", "seq", "delete"])
async def test_business_mutation_of_frozen_input_is_rejected(recorded, mutation):
    async def after_node(ctx, *, node, result):
        if isinstance(node, ModelRequestNode):
            if mutation == "parts":
                ctx.messages[0].parts.clear()
            elif mutation == "metadata":
                ctx.messages[0].metadata["business"] = "changed"
            elif mutation == "seq":
                ctx.messages[0].metadata["seq"] = 9
            else:
                ctx.messages.pop(0)
        return result

    with pytest.raises(ValueError, match="Published messages"):
        await run(
            recording_agent(TestModel(custom_output_text="done"), Hooks(after_node_run=after_node)),
            make_deps(),
        )
    assert len(recorded) == 1


async def test_after_run_cannot_rewrite_the_published_answer(recorded):
    async def rewrite(ctx, *, result):
        result.all_messages()[-1].parts = [TextPart("changed")]
        return result

    deps = make_deps()
    result = await run(
        recording_agent(TestModel(custom_output_text="done"), Hooks(after_run=rewrite)), deps
    )
    with pytest.raises(ValueError, match="Published messages"):
        await deps.recorder.record_messages(
            deps,
            result.all_messages(),
            checkpoint="run_completed",
            next=DoneNext(output=result.output),
            release=True,
        )
    assert isinstance(recorded[-1].state.next, FinishNext)


async def test_sealed_numeric_gap_is_not_reused_and_sdk_send_view_is_separate(recorded):
    history = [
        ModelRequest([UserPromptPart("old")], metadata={"seq": 5}),
        ModelResponse([TextPart("old reply")], metadata={"seq": 10}),
    ]
    deps = make_deps(history)
    result = await run(recording_agent(TestModel(custom_output_text="done")), deps, history=history)
    assert [m.seq for s in recorded for m in s.messages] == [11, 12]
    assert [m.metadata["seq"] for m in result.all_messages()] == [5, 10, 11, 12]
    assert history[0].metadata == {"seq": 5}


async def test_new_message_cannot_claim_an_old_position(recorded):
    request = ModelRequest([UserPromptPart("new")], metadata={"seq": 0})
    deps = make_deps()
    with pytest.raises(ValueError, match="already assigned"):
        await deps.recorder.record_messages(
            deps, [request], checkpoint="before_model_request", next=ModelRequestNext()
        )
    assert recorded == []


@pytest.mark.parametrize("kind", ["model_request", "call_tools", "finish"])
async def test_explicit_continuation_preserves_identity_and_skips_completed_nodes(recorded, kind):
    request = ModelRequest(
        [UserPromptPart("original input")],
        metadata={"seq": 5},
        run_id="old-sdk-run",
        conversation_id="conversation",
    )
    response = ModelResponse(
        [TextPart("saved answer")],
        metadata={"seq": 10},
        run_id="old-sdk-run",
        conversation_id="conversation",
    )
    if kind == "model_request":
        messages, checkpoint, next = [request], "before_model_request", ModelRequestNext()
    elif kind == "call_tools":
        messages, checkpoint, next = [request, response], "model_node_completed", CallToolsNext()
    else:
        messages, checkpoint, next = (
            [request, response],
            "tools_node_completed",
            FinishNext(output="saved processed answer"),
        )
    saved = Checkpoint(checkpoint=checkpoint, messages=messages, next=next)
    deps = make_deps(messages, resume=saved)
    model = TestModel(custom_output_text="fresh answer")
    result = await run(
        recording_agent(model, instructions="Instructions must not force another request."),
        deps,
        prompt=None,
        history=messages,
    )
    assert result.output == (
        "fresh answer"
        if kind == "model_request"
        else "saved answer"
        if kind == "call_tools"
        else "saved processed answer"
    )
    assert result.all_messages()[0].run_id == "old-sdk-run"
    assert result.all_messages()[0].conversation_id == "conversation"
    assert (
        sum(
            isinstance(p, UserPromptPart) and p.content == "original input"
            for m in result.all_messages()
            for p in m.parts
        )
        == 1
    )
    assert all(s.state.checkpoint != "before_model_request" for s in recorded)
    assert len(recorded) == (2 if kind == "model_request" else 1 if kind == "call_tools" else 0)


@pytest.mark.parametrize("invalid", ["name", "arguments"])
async def test_restored_tool_batch_validates_every_call_before_execution(recorded, invalid):
    calls = []

    def work(value: int) -> str:
        calls.append(value)
        return "ok"

    messages = [
        ModelRequest([UserPromptPart("work")], metadata={"seq": 0}),
        ModelResponse(
            [
                ToolCallPart("work", {"value": 1}, "first"),
                ToolCallPart(
                    "missing" if invalid == "name" else "work",
                    {"value": "invalid"} if invalid == "arguments" else {"value": 2},
                    "second",
                ),
            ],
            metadata={"seq": 1},
        ),
    ]
    saved = Checkpoint(checkpoint="model_node_completed", messages=messages, next=CallToolsNext())
    with pytest.raises(ValueError):
        await run(
            recording_agent(TestModel(), tools=[work]),
            make_deps(messages, resume=saved),
            prompt=None,
            history=messages,
        )
    assert calls == [] and recorded == []


async def test_shared_static_agent_keeps_recorders_independent(recorded):
    agent = recording_agent(TestModel(custom_output_text="done"))
    deps = [make_deps(), make_deps()]
    results = await asyncio.gather(*(run(agent, d) for d in deps))
    assert [[m.metadata["seq"] for m in r.all_messages()] for r in results] == [[0, 1], [0, 1]]
    assert len({s.session_id for s in recorded}) == 2


@pytest.mark.parametrize("kind", ["model_request", "call_tools", "finish"])
async def test_resume_keeps_new_before_run_messages_after_the_frozen_prefix(recorded, kind):
    history: list[ModelMessage] = [ModelRequest([UserPromptPart("original")], metadata={"seq": 0})]
    if kind == "model_request":
        saved = Checkpoint(
            checkpoint="before_model_request", messages=history, next=ModelRequestNext()
        )
    else:
        history.append(ModelResponse([TextPart("saved answer")], metadata={"seq": 1}))
        saved = Checkpoint(
            checkpoint="model_node_completed" if kind == "call_tools" else "tools_node_completed",
            messages=history,
            next=CallToolsNext() if kind == "call_tools" else FinishNext(output="saved answer"),
        )

    async def append(ctx):
        ctx.messages.append(ModelRequest([UserPromptPart("hook addition")]))

    deps = make_deps(history, resume=saved)
    result = await run(
        recording_agent(TestModel(custom_output_text="done"), Hooks(before_run=append)),
        deps,
        prompt=None,
        history=history,
    )
    await deps.recorder.record_messages(
        deps, result.all_messages(), checkpoint="run_completed", next=DoneNext(output=result.output)
    )
    assert (
        sum(
            isinstance(p, UserPromptPart) and p.content == "hook addition"
            for m in recorded[-1].state.messages
            for p in m.parts
        )
        == 1
    )
    assert recorded[-1].state.messages[: len(history)] == history


@pytest.mark.parametrize("streaming", [False, True])
async def test_model_retry_request_is_saved_and_acknowledged_before_next_dispatch(
    recorded, streaming
):
    retried = []

    async def retry(ctx, *, request_context, response):
        if not retried:
            retried.append(True)
            raise ModelRetry("retry this response")
        return response

    async def drain(ctx, events):
        async for _ in events:
            pass

    async def respond(messages, info):
        return ModelResponse([TextPart("done")])

    async def stream(messages, info):
        yield "done"

    deps = make_deps()
    result = await run(
        recording_agent(
            FunctionModel(respond, stream_function=stream), Hooks(after_model_request=retry)
        ),
        deps,
        event_stream_handler=drain if streaming else None,
    )
    saved_retry = recorded[1].state
    assert saved_retry.checkpoint == "model_node_completed"
    assert saved_retry.next == ModelRequestNext()
    assert saved_retry.messages[-1].parts[0].part_kind == "retry-prompt"
    assert [m.metadata["seq"] for m in result.all_messages()] == [0, 1, 2, 3]
    assert recorded[-1].state.messages[2] == saved_retry.messages[2]


async def test_finish_preserves_output_tool_receipt_and_skips_output_validation(recorded):
    original_deps = make_deps()
    agent = recording_agent(
        TestModel(custom_output_args="candidate"),
        output_type=ToolOutput(str, name="final_text"),
    )
    result = await run(agent, original_deps)
    assert result.output == "candidate"
    finish = recorded[-1].state
    assert isinstance(finish.next, FinishNext) and finish.next.tool_name == "final_text"
    assert finish.messages[-1].parts[0].part_kind == "tool-return"
    assert finish.messages[-1].metadata == {"seq": 2}
    recorded.clear()
    resumed = await run(
        agent, make_deps(finish.messages, resume=finish), prompt=None, history=finish.messages
    )
    assert resumed.output == "candidate"
    assert resumed.all_messages() == finish.messages
    assert recorded == []

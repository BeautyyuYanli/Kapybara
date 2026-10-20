"""Recording boundaries follow real SDK nodes; only database persistence is stubbed."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from pydantic_ai import Agent, ModelRequestNode, RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering, Hooks
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.test import TestModel
from pydantic_ai.run import AgentRunResult
from pydantic_ai.usage import RunUsage
from sqlalchemy.ext.asyncio import async_sessionmaker

from kapy.agent_output import AgentOutputService
from kapy.runner import (
    MessageRecordCapability,
    Recorder,
    RunnerDeps,
)

pytestmark = pytest.mark.asyncio


def make_deps() -> RunnerDeps:
    return RunnerDeps(
        session_id=uuid4(),
        session_factory=Mock(spec=async_sessionmaker),
        output_service=Mock(spec=AgentOutputService),
        activity_id=uuid4(),
        activity_run_id="test-run",
    )


@pytest.fixture
def recorded(monkeypatch) -> list[SimpleNamespace]:
    batches = []

    async def record(self, deps, messages):
        batches.append(SimpleNamespace(session_id=deps.session_id, messages=deepcopy(messages)))
        await asyncio.sleep(0)

    monkeypatch.setattr(Recorder, "persist_messages", record)
    return batches


def metadata(message: ModelMessage) -> dict[str, Any]:
    assert message.metadata is not None
    return message.metadata


def request_hook(recorder: Recorder) -> Hooks:
    return Hooks(
        before_model_request=recorder.predict_response_seq,
        ordering=CapabilityOrdering(position="innermost", requires=[MessageRecordCapability]),
    )


class FinishMessages(AbstractCapability[RunnerDeps]):
    """Prepare inputs and finalize output before post-node recording."""

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="outermost")

    async def wrap_model_request(self, ctx, *, request_context, handler):
        request = ctx.messages[-1]
        request.metadata = {**(request.metadata or {}), "prepared": True}
        return await handler(request_context)

    async def before_model_request(
        self, ctx: RunContext[RunnerDeps], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        request = request_context.messages[-1]
        assert isinstance(request, ModelRequest)
        request.parts = [*request.parts, UserPromptPart("late input")]
        return replace(
            request_context,
            model_request_parameters=replace(
                request_context.model_request_parameters,
                instruction_parts=[InstructionPart("confirmed instructions")],
            ),
        )

    async def after_node_run(self, ctx, *, node, result):
        if isinstance(node, ModelRequestNode):
            response = ctx.messages[-1]
            assert isinstance(response, ModelResponse)
            response.parts = [TextPart("finished")]
        return result

    async def after_run(
        self, ctx: RunContext[RunnerDeps], *, result: AgentRunResult[Any]
    ) -> AgentRunResult[Any]:
        result.all_messages().append(ModelRequest(parts=[UserPromptPart("next run")]))
        return result


async def test_prediction_then_node_recording_leaves_after_run_tail_unrecorded(recorded):
    from pydantic_ai.models.function import FunctionModel

    predicted = []

    async def respond(messages, info):
        predicted.append(deps.response_seq)
        assert messages[-1].metadata == {"prepared": True}
        assert [part.content for part in messages[-1].parts] == ["go", "late input"]
        assert messages[-1].instructions == "confirmed instructions"
        assert recorded == []
        return ModelResponse(parts=[TextPart("done")])

    deps = make_deps()
    recorder = Recorder()
    agent = Agent(
        FunctionModel(respond),
        deps_type=RunnerDeps,
        capabilities=[MessageRecordCapability(recorder), FinishMessages(), request_hook(recorder)],
    )
    result = await agent.run("go", deps=deps)
    assert [[m.seq for m in b.messages] for b in recorded] == [[0, 1]]
    assert recorded[0].messages[0].message.metadata == {
        "prepared": True,
        "seq": 0,
    }
    assert recorded[0].messages[0].message.instructions == "confirmed instructions"
    assert recorded[0].messages[1].message.parts == [TextPart("finished")]
    assert [m.metadata for m in result.all_messages()][-2:] == [
        {"seq": 1},
        None,
    ]
    assert predicted == [1]


async def test_prediction_preserves_working_history_and_request_context(recorded):
    messages: list[ModelMessage] = [
        ModelResponse([TextPart("anchor")], metadata={"seq": 5}),
        ModelRequest([UserPromptPart("second")]),
        ModelRequest([UserPromptPart("third")]),
    ]
    outgoing: list[ModelMessage] = [ModelRequest([UserPromptPart("model-only view")])]
    model = TestModel()
    ctx = RunContext(deps=make_deps(), model=model, usage=RunUsage(), messages=messages)
    request_context = ModelRequestContext(
        model=model,
        messages=outgoing,
        model_settings=None,
        model_request_parameters=ModelRequestParameters(),
    )
    original_history, original_outgoing = deepcopy(messages), deepcopy(outgoing)

    assert await Recorder().predict_response_seq(ctx, request_context) is request_context

    assert ctx.deps.response_seq == 8
    assert ctx.messages is messages and ctx.messages == original_history
    assert request_context.messages is outgoing and outgoing == original_outgoing
    assert recorded == []


async def test_tools_and_output_tool_tail_use_same_recording_boundaries(recorded):
    recorder = Recorder()
    agent = Agent(
        TestModel(custom_output_args=["one"]),
        output_type=list[str],
        deps_type=RunnerDeps,
        capabilities=[MessageRecordCapability(recorder), request_hook(recorder)],
    )

    @agent.tool_plain
    def work() -> str:
        return "ok"

    result = await agent.run("go", deps=make_deps())
    assert result.output == ["one"]
    assert [[m.seq for m in b.messages] for b in recorded] == [[0, 1], [2, 3]]
    tail = result.all_messages()[-1]
    assert isinstance(tail.parts[0], ToolReturnPart) and tail.metadata is None

    restored = await agent.run("again", deps=make_deps(), message_history=result.all_messages())
    assert [m.seq for m in recorded[2].messages][:2] == [4, 5]
    assert isinstance(recorded[2].messages[0].message.parts[0], ToolReturnPart)
    assert restored.output == ["one"]


async def test_restore_tail_starts_after_last_recorded_position(recorded):
    recorder = Recorder()
    agent = Agent(
        TestModel(),
        deps_type=RunnerDeps,
        capabilities=[MessageRecordCapability(recorder), request_hook(recorder)],
    )
    history = [
        ModelRequest(parts=[UserPromptPart("first")], metadata={"seq": 5}),
        ModelResponse(parts=[TextPart("reply")], metadata={"seq": 10}),
        ModelRequest(parts=[UserPromptPart("second")]),
        ModelRequest(parts=[UserPromptPart("third")]),
    ]
    result = await agent.run("new", deps=make_deps(), message_history=history)
    assert [[m.seq for m in b.messages] for b in recorded] == [[11, 12, 13]]
    assert [part.content for part in recorded[0].messages[0].message.parts] == [
        "second",
        "third",
    ]
    request = recorded[0].messages[1].message
    assert isinstance(request, ModelRequest)
    part = request.parts[0]
    assert isinstance(part, UserPromptPart) and part.content == "new"
    assert [metadata(m)["seq"] for m in result.all_messages()] == [5, 10, 11, 12, 13]


async def test_recorded_positions_skip_retries_and_preserve_gaps(recorded):
    messages = [
        ModelResponse(parts=[TextPart("old")], metadata={"seq": 5}),
        ModelRequest(parts=[UserPromptPart("tail")], metadata={"source": "business"}),
    ]
    recorder, deps = Recorder(), make_deps()
    await recorder.record_messages(deps, messages)
    assert [m.metadata for m in messages] == [
        {"seq": 5},
        {"source": "business", "seq": 6},
    ]
    await recorder.record_messages(deps, messages)
    assert [[m.seq for m in b.messages] for b in recorded] == [[6]]


@pytest.mark.parametrize(
    "metadata,error",
    [
        ({"seq": True}, "nonnegative integer"),
        ({"seq": -1}, "nonnegative integer"),
        ({"seq": "1"}, "nonnegative integer"),
        ({"seq": None}, "nonnegative integer"),
    ],
)
async def test_invalid_metadata_rejected(recorded, metadata, error):
    with pytest.raises(UserError, match=error):
        await Recorder().record_messages(make_deps(), [ModelResponse(parts=[], metadata=metadata)])
    assert recorded == []


@pytest.mark.parametrize("seq", [5, 4])
async def test_recorded_positions_must_increase(recorded, seq):
    messages: list[ModelMessage] = [
        ModelResponse(parts=[], metadata={"seq": value}) for value in (5, seq)
    ]
    with pytest.raises(UserError, match="strictly increasing"):
        await Recorder().record_messages(make_deps(), messages)
    assert recorded == []


async def test_failed_recording_keeps_live_metadata_and_independent_copy(monkeypatch):
    message = ModelRequest(parts=[UserPromptPart("original")], metadata={"source": "business"})

    async def fail(self, deps, messages):
        assert messages[0].seq == 0
        messages[0].message.parts.clear()
        assert message.metadata == {"source": "business"} and message.parts
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(Recorder, "persist_messages", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await Recorder().record_messages(make_deps(), [message])
    assert message.metadata == {"source": "business"} and message.parts


async def test_model_failure_records_no_history(recorded):
    from pydantic_ai.models.function import FunctionModel

    deps = make_deps()
    recorder = Recorder()

    async def fail(messages, info):
        assert deps.response_seq == 1
        raise RuntimeError("model failed")

    agent = Agent(
        FunctionModel(fail),
        deps_type=RunnerDeps,
        capabilities=[MessageRecordCapability(recorder), request_hook(recorder)],
    )
    with pytest.raises(RuntimeError, match="model failed"):
        await agent.run("go", deps=deps)
    assert recorded == []


async def test_recording_failure_occurs_after_model_dispatch(monkeypatch):
    from pydantic_ai.models.function import FunctionModel

    deps = make_deps()
    recorder = Recorder()
    dispatched = []

    async def fail(self, deps, messages):
        assert [m.seq for m in messages] == [0, 1]
        raise RuntimeError("database unavailable")

    async def respond(messages, info):
        dispatched.append(messages)
        return ModelResponse(parts=[TextPart("done")])

    monkeypatch.setattr(Recorder, "persist_messages", fail)
    agent = Agent(
        FunctionModel(respond),
        deps_type=RunnerDeps,
        capabilities=[MessageRecordCapability(recorder), request_hook(recorder)],
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await agent.run("go", deps=deps)
    assert len(dispatched) == 1
    assert all("seq" not in (message.metadata or {}) for message in dispatched[0])


async def test_shared_agent_runs_keep_independent_positions(recorded):
    recorder = Recorder()
    agent = Agent(
        TestModel(),
        deps_type=RunnerDeps,
        capabilities=[MessageRecordCapability(recorder), request_hook(recorder)],
    )
    results = await asyncio.gather(
        agent.run("first", deps=make_deps()), agent.run("second", deps=make_deps())
    )
    assert [[metadata(m)["seq"] for m in r.all_messages()] for r in results] == [[0, 1], [0, 1]]
    assert len({batch.session_id for batch in recorded}) == 2

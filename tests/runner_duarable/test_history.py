"""Recording boundaries follow real SDK nodes; only Activity persistence is stubbed."""

import asyncio
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from pydantic_ai import Agent, ModelRequestNode, RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering, Hooks
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.run import AgentRunResult

from kapy.runner_duarable import DurableExecutionConfig, MessageRecordCapability, RunnerDeps
from kapy.runner_duarable.recording import record_before_model_request
from kapy.runner_duarable.types import MessageBatch

pytestmark = pytest.mark.asyncio


def make_deps() -> RunnerDeps:
    return RunnerDeps(
        config=DurableExecutionConfig(
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="pydantic_ai.models.openai:OpenAIChatModel",
            model_name="test",
            api_key="test-secret",
        ),
        session_id=uuid4(),
    )


@pytest.fixture
def recorded(monkeypatch) -> list[MessageBatch]:
    batches = []

    async def record(name, data, *, start_to_close_timeout):
        assert name == "kapy.record_history"
        assert start_to_close_timeout.total_seconds() == 30
        batches.append(deepcopy(data))
        await asyncio.sleep(0)

    monkeypatch.setattr("kapy.runner_duarable.recording.workflow.execute_activity", record)
    return batches


def metadata(message: ModelMessage) -> dict[str, Any]:
    assert message.metadata is not None
    return message.metadata


def request_hook() -> Hooks:
    return Hooks(
        model_request=record_before_model_request,
        ordering=CapabilityOrdering(position="innermost", requires=[MessageRecordCapability]),
    )


class FinishMessages(AbstractCapability[RunnerDeps]):
    """Business hooks finalize messages before recording, even when also outermost."""

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="outermost")

    async def wrap_model_request(self, ctx, *, request_context, handler):
        request = ctx.messages[-1]
        request.metadata = {**(request.metadata or {}), "prepared": True}
        return await handler(request_context)

    async def after_node_run(self, ctx, *, node, result):
        if isinstance(node, ModelRequestNode):
            request = next(m for m in reversed(ctx.messages) if isinstance(m, ModelRequest))
            request.metadata = {**(request.metadata or {}), "finished": True}
        return result

    async def after_run(
        self, ctx: RunContext[RunnerDeps], *, result: AgentRunResult[Any]
    ) -> AgentRunResult[Any]:
        result.all_messages().append(ModelRequest(parts=[UserPromptPart("next run")]))
        return result


async def test_three_boundaries_finalize_then_record_and_predict_response(recorded):
    from pydantic_ai.models.function import FunctionModel

    predicted = []

    async def respond(messages, info):
        predicted.append(deps.response_seq)
        assert messages[-1].metadata == {"prepared": True, "seq": 0, "authoritative": False}
        assert recorded[-1].messages[0].message == messages[-1]
        return ModelResponse(parts=[TextPart("done")])

    deps = make_deps()
    agent = Agent(
        FunctionModel(respond),
        deps_type=RunnerDeps,
        capabilities=[MessageRecordCapability(), FinishMessages(), request_hook()],
    )
    result = await agent.run("go", deps=deps)
    assert [[(m.seq, m.authoritative) for m in b.messages] for b in recorded] == [
        [(0, False)],
        [(0, True), (1, True)],
        [(2, False)],
    ]
    assert recorded[1].messages[0].message.metadata == {
        "prepared": True,
        "finished": True,
        "seq": 0,
        "authoritative": True,
    }
    assert [m.metadata for m in result.all_messages()][-2:] == [
        {"seq": 1, "authoritative": True},
        {"seq": 2, "authoritative": False},
    ]
    assert predicted == [1] and deps.response_seq is None


async def test_tools_and_output_tool_tail_use_same_recording_boundaries(recorded):
    agent = Agent(
        TestModel(custom_output_args=["one"]),
        output_type=list[str],
        deps_type=RunnerDeps,
        capabilities=[MessageRecordCapability(), request_hook()],
    )

    @agent.tool_plain
    def work() -> str:
        return "ok"

    result = await agent.run("go", deps=make_deps())
    assert result.output == ["one"]
    assert [[(m.seq, m.authoritative) for m in b.messages] for b in recorded] == [
        [(0, False)],
        [(0, True), (1, True)],
        [(2, False)],
        [(2, True), (3, True)],
        [(4, False)],
    ]
    assert isinstance(recorded[-1].messages[0].message.parts[0], ToolReturnPart)


async def test_restore_merged_non_authoritative_tail_restarts_after_authority(recorded):
    agent = Agent(
        TestModel(),
        deps_type=RunnerDeps,
        capabilities=[MessageRecordCapability(), request_hook()],
    )
    history = [
        ModelRequest(parts=[UserPromptPart("first")], metadata={"seq": 5, "authoritative": True}),
        ModelResponse(parts=[TextPart("reply")], metadata={"seq": 10, "authoritative": True}),
        ModelRequest(
            parts=[UserPromptPart("second")], metadata={"seq": 90, "authoritative": False}
        ),
        ModelRequest(parts=[UserPromptPart("third")], metadata={"seq": 90}),
    ]
    result = await agent.run("new", deps=make_deps(), message_history=history)
    assert [[m.seq for m in b.messages] for b in recorded] == [[11, 12], [11, 12, 13]]
    assert [part.content for part in recorded[0].messages[0].message.parts] == ["second", "third"]
    assert [metadata(m)["seq"] for m in result.all_messages()] == [5, 10, 11, 12, 13]
    assert all(metadata(m)["authoritative"] for m in result.all_messages())


async def test_non_authoritative_positions_never_advance_anchor(recorded):
    messages = [
        ModelResponse(parts=[TextPart("old")], metadata={"seq": 90}),
        ModelRequest(parts=[UserPromptPart("tail")], metadata={"seq": 90, "authoritative": False}),
    ]
    recorder, deps = MessageRecordCapability(), make_deps()
    assert await recorder.record_messages(deps, messages, authoritative=False) == 2
    assert [m.metadata for m in messages] == [
        {"seq": 0, "authoritative": False},
        {"seq": 1, "authoritative": False},
    ]
    assert await recorder.record_messages(deps, messages, authoritative=True) == 2
    assert [[m.seq for m in b.messages] for b in recorded] == [[0, 1], [0, 1]]
    assert await recorder.record_messages(deps, messages, authoritative=False) == 2
    assert len(recorded) == 2


@pytest.mark.parametrize(
    "metadata,error",
    [
        ({"seq": True}, "nonnegative integer"),
        ({"seq": -1}, "nonnegative integer"),
        ({"seq": "1"}, "nonnegative integer"),
        ({"seq": None}, "nonnegative integer"),
        ({"authoritative": "true"}, "boolean"),
        ({"authoritative": True}, "requires metadata.seq"),
    ],
)
async def test_invalid_metadata_rejected(recorded, metadata, error):
    with pytest.raises(UserError, match=error):
        await MessageRecordCapability().record_messages(
            make_deps(), [ModelResponse(parts=[], metadata=metadata)], authoritative=False
        )
    assert recorded == []


@pytest.mark.parametrize("seq", [5, 4])
async def test_authoritative_marks_must_increase(recorded, seq):
    messages: list[ModelMessage] = [
        ModelResponse(parts=[], metadata={"seq": value, "authoritative": True})
        for value in (5, seq)
    ]
    with pytest.raises(UserError, match="strictly increasing"):
        await MessageRecordCapability().record_messages(make_deps(), messages, authoritative=True)
    assert recorded == []


async def test_failed_recording_keeps_live_metadata_and_independent_copy(monkeypatch):
    message = ModelRequest(parts=[UserPromptPart("original")], metadata={"source": "business"})

    async def fail(name, data, **kwargs):
        assert data.messages[0].seq == 0 and data.messages[0].authoritative
        data.messages[0].message.parts.clear()
        assert message.metadata == {"source": "business"} and message.parts
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("kapy.runner_duarable.recording.workflow.execute_activity", fail)
    with pytest.raises(RuntimeError, match="database unavailable"):
        await MessageRecordCapability().record_messages(make_deps(), [message], authoritative=True)
    assert message.metadata == {"source": "business"} and message.parts


async def test_model_failure_clears_prediction_and_retains_provisional_request(recorded):
    from pydantic_ai.models.function import FunctionModel

    deps = make_deps()

    async def fail(messages, info):
        assert deps.response_seq == 1
        raise RuntimeError("model failed")

    agent = Agent(
        FunctionModel(fail),
        deps_type=RunnerDeps,
        capabilities=[MessageRecordCapability(), request_hook()],
    )
    with pytest.raises(RuntimeError, match="model failed"):
        await agent.run("go", deps=deps)
    assert deps.response_seq is None
    assert [[(m.seq, m.authoritative) for m in b.messages] for b in recorded] == [[(0, False)]]


async def test_shared_agent_runs_keep_independent_positions(recorded):
    agent = Agent(
        TestModel(), deps_type=RunnerDeps, capabilities=[MessageRecordCapability(), request_hook()]
    )
    results = await asyncio.gather(
        agent.run("first", deps=make_deps()), agent.run("second", deps=make_deps())
    )
    assert [[metadata(m)["seq"] for m in r.all_messages()] for r in results] == [[0, 1], [0, 1]]
    assert len({batch.session_id for batch in recorded}) == 2


@pytest.mark.parametrize("leading_authority", [False, True])
@pytest.mark.parametrize("with_tail", [False, True])
async def test_recorder_ignores_provisional_messages_before_last_anchor(
    recorded, leading_authority, with_tail
):
    messages: list[ModelMessage] = []
    if leading_authority:
        messages.append(
            ModelResponse([TextPart("first")], metadata={"seq": 1, "authoritative": True})
        )
    ignored = ModelRequest(
        [UserPromptPart("ignored")], metadata={"seq": 90, "authoritative": False}
    )
    messages.extend(
        [ignored, ModelResponse([TextPart("anchor")], metadata={"seq": 5, "authoritative": True})]
    )
    original = deepcopy(messages)
    if with_tail:
        messages.append(
            ModelRequest([UserPromptPart("tail")], metadata={"seq": 2, "authoritative": False})
        )
    next_seq = await MessageRecordCapability().record_messages(
        make_deps(), messages, authoritative=True
    )
    assert next_seq == (7 if with_tail else 6)
    assert messages[: len(original)] == original
    if with_tail:
        assert len(recorded) == 1
        assert [(m.seq, m.authoritative) for m in recorded[0].messages] == [(6, True)]
        assert metadata(messages[-1]) == {"seq": 6, "authoritative": True}
    else:
        assert recorded == []

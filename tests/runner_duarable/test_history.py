"""History marks follow real SDK nodes; only the Temporal persistence boundary is stubbed."""

import asyncio
from copy import deepcopy
from typing import Any
from uuid import uuid4

import pytest
from pydantic_ai import Agent, ModelRequestNode, RunContext
from pydantic_ai.capabilities import AbstractCapability, AgentNode, CapabilityOrdering, NodeResult
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

from kapy.runner_duarable import DurableExecutionConfig, HistoryRecordCapability, RunnerDeps
from kapy.runner_duarable.types import RecordHistoryInput

pytestmark = pytest.mark.asyncio


def make_deps(next_seq: int | None = None) -> RunnerDeps:
    return RunnerDeps(
        config=DurableExecutionConfig(
            provider_class="pydantic_ai.providers.openai:OpenAIProvider",
            model_class="pydantic_ai.models.openai:OpenAIChatModel",
            model_name="test",
            api_key="test-secret",
        ),
        session_id=uuid4(),
        next_seq=next_seq,
    )


@pytest.fixture
def recorded(monkeypatch) -> list[RecordHistoryInput]:
    batches = []

    async def record(name, data, *, start_to_close_timeout):
        assert name == "kapy.record_history"
        assert start_to_close_timeout.total_seconds() == 30
        batches.append(deepcopy(data))
        await asyncio.sleep(0)

    monkeypatch.setattr("kapy.runner_duarable.history.workflow.execute_activity", record)
    return batches


def seqs(messages: list[ModelMessage]) -> list[int]:
    result = []
    for message in messages:
        assert message.metadata is not None
        result.append(message.metadata["seq"])
    return result


class FinishRequest(AbstractCapability[RunnerDeps]):
    """Business hooks must run before the recorder, even if both request outermost."""

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="outermost")

    async def after_node_run(
        self,
        ctx: RunContext[RunnerDeps],
        *,
        node: AgentNode[RunnerDeps],
        result: NodeResult[RunnerDeps],
    ) -> NodeResult[RunnerDeps]:
        if isinstance(node, ModelRequestNode):
            request = next(m for m in reversed(ctx.messages) if isinstance(m, ModelRequest))
            request.metadata = {**(request.metadata or {}), "source": "business"}
        return result

    async def after_run(
        self, ctx: RunContext[RunnerDeps], *, result: AgentRunResult[Any]
    ) -> AgentRunResult[Any]:
        request = next(m for m in reversed(result.all_messages()) if isinstance(m, ModelRequest))
        request.parts = [*request.parts, UserPromptPart("final request content")]
        return result


async def test_tools_explicit_start_and_final_request_overwrite(recorded):
    agent = Agent(
        TestModel(),
        deps_type=RunnerDeps,
        capabilities=[HistoryRecordCapability(), FinishRequest()],
    )

    @agent.tool_plain
    def work() -> str:
        return "ok"

    deps = make_deps(100)
    result = await agent.run("go", deps=deps)
    assert [seqs(batch.messages) for batch in recorded] == [[100, 101], [102, 103], [102]]
    assert seqs(result.all_messages()) == [100, 101, 102, 103]
    assert deps.next_seq == 100
    assert all(batch.session_id == deps.session_id for batch in recorded)
    initial_request, final_request = recorded[1].messages[0], recorded[-1].messages[0]
    assert initial_request.metadata == final_request.metadata == {"source": "business", "seq": 102}
    assert len(final_request.parts) == len(initial_request.parts) + 1
    assert final_request.parts[-1] == UserPromptPart(
        "final request content", timestamp=final_request.parts[-1].timestamp
    )


async def test_output_tool_closing_request_recorded_once(recorded):
    agent = Agent(
        TestModel(custom_output_args=["one"]),
        output_type=list[str],
        deps_type=RunnerDeps,
        capabilities=[HistoryRecordCapability()],
    )
    result = await agent.run("go", deps=make_deps())
    assert result.output == ["one"]
    assert [seqs(batch.messages) for batch in recorded] == [[0, 1], [2]]
    assert isinstance(recorded[-1].messages[0].parts[0], ToolReturnPart)
    assert seqs(result.all_messages()) == [0, 1, 2]


async def test_restore_merged_suffix_uses_last_remaining_mark(recorded):
    agent = Agent(TestModel(), deps_type=RunnerDeps, capabilities=[HistoryRecordCapability()])
    history = [
        ModelRequest(parts=[UserPromptPart("first")], metadata={"seq": 8}),
        ModelResponse(parts=[TextPart("reply")], metadata={"seq": 9}),
        ModelRequest(parts=[UserPromptPart("second")], metadata={"seq": 10}),
        ModelRequest(parts=[UserPromptPart("third")], metadata={"seq": 11}),
    ]
    result = await agent.run("new", deps=make_deps(), message_history=history)
    assert [seqs(batch.messages) for batch in recorded] == [[10, 11, 12], [11]]
    merged = recorded[0].messages[0]
    assert [part.content for part in merged.parts] == ["second", "third"]
    assert seqs(result.all_messages()) == [8, 9, 10, 11, 12]


@pytest.mark.parametrize("next_seq", [None, 50])
async def test_no_anchor_requires_explicit_start_for_nonempty_history(recorded, next_seq):
    agent = Agent(TestModel(), deps_type=RunnerDeps, capabilities=[HistoryRecordCapability()])
    history = [
        ModelRequest(parts=[UserPromptPart("first")], metadata={"seq": 10}),
        ModelRequest(parts=[UserPromptPart("second")], metadata={"seq": 11}),
    ]
    if next_seq is None:
        with pytest.raises(UserError, match="no seq anchor"):
            await agent.run("new", deps=make_deps(), message_history=history)
        assert recorded == []
    else:
        result = await agent.run("new", deps=make_deps(next_seq), message_history=history)
        assert seqs(result.all_messages()) == [50, 51, 52]


@pytest.mark.parametrize("start", [None, 100])
async def test_restore_numbered_history_and_unnumbered_tail(recorded, start):
    history = [
        ModelRequest(parts=[UserPromptPart("old")], metadata={"seq": 5}),
        ModelResponse(parts=[TextPart("reply")], metadata={"seq": 10}),
        ModelRequest(parts=[UserPromptPart("pending")]),
    ]
    agent = Agent(TestModel(), deps_type=RunnerDeps, capabilities=[HistoryRecordCapability()])
    result = await agent.run("new", deps=make_deps(start), message_history=history)
    expected = start if start is not None else 11
    assert seqs(result.all_messages()) == [5, 10, expected, expected + 1, expected + 2]


@pytest.mark.parametrize(
    "metadata,start,error",
    [
        ({"seq": True}, None, "nonnegative integer"),
        ({"seq": -1}, None, "nonnegative integer"),
        ({"seq": 1.5}, None, "nonnegative integer"),
        ({"seq": "1"}, None, "nonnegative integer"),
        ({"seq": None}, None, "nonnegative integer"),
        ({"seq": 5}, 5, "greater than"),
        ({"seq": 5}, 4, "greater than"),
    ],
)
async def test_invalid_marks_and_colliding_start_fail(recorded, metadata, start, error):
    agent = Agent(TestModel(), deps_type=RunnerDeps, capabilities=[HistoryRecordCapability()])
    history = [ModelResponse(parts=[TextPart("old")], metadata=metadata)]
    with pytest.raises(UserError, match=error):
        await agent.run("go", deps=make_deps(start), message_history=history)
    assert recorded == []


async def test_lost_all_marks_after_first_record_cannot_reuse_explicit_start(recorded):
    class LoseMarks(AbstractCapability[RunnerDeps]):
        async def after_node_run(self, ctx, *, node, result):
            if isinstance(node, ModelRequestNode) and len(ctx.messages) > 2:
                for message in ctx.messages:
                    message.metadata = None
            return result

    agent = Agent(
        TestModel(),
        deps_type=RunnerDeps,
        capabilities=[HistoryRecordCapability(), LoseMarks()],
    )

    @agent.tool_plain
    def work() -> str:
        return "ok"

    with pytest.raises(UserError, match="no seq anchor"):
        await agent.run("go", deps=make_deps(100))
    assert [seqs(batch.messages) for batch in recorded] == [[100, 101]]


async def test_failed_activity_does_not_mark_live_history(monkeypatch):
    observed = []

    class Observe(AbstractCapability[RunnerDeps]):
        async def after_node_run(self, ctx, *, node, result):
            if isinstance(node, ModelRequestNode):
                observed.extend(ctx.messages)
            return result

    async def fail(name, data, **kwargs):
        assert seqs(data.messages) == [50, 51]
        assert all(message.metadata is None for message in observed)
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("kapy.runner_duarable.history.workflow.execute_activity", fail)
    agent = Agent(
        TestModel(), deps_type=RunnerDeps, capabilities=[HistoryRecordCapability(), Observe()]
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await agent.run("go", deps=make_deps(50))
    assert len(observed) == 2
    assert all(message.metadata is None for message in observed)


async def test_same_agent_isolates_concurrent_runs(recorded):
    agent = Agent(TestModel(), deps_type=RunnerDeps, capabilities=[HistoryRecordCapability()])
    results = await asyncio.gather(
        agent.run("first", deps=make_deps()), agent.run("second", deps=make_deps())
    )
    assert [seqs(result.all_messages()) for result in results] == [[0, 1], [0, 1]]
    assert len({batch.session_id for batch in recorded}) == 2

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from kapy.agent import runner as runner_module
from kapy.state import CheckpointWrite, SessionInput

from .test_runner import Caller, Context, runner  # type: ignore[missing-import]


class CancelAfterFinalCheckpoint(Context):
    interrupted = False

    async def checkpoint(self, write: CheckpointWrite) -> str:
        cursor = await super().checkpoint(write)
        cycle = cast(Any, write.state.data["cycles"])[-1]
        if not self.interrupted and cycle.get("pending_final"):
            self.interrupted = True
            raise asyncio.CancelledError
        return cursor


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["text", "wait"])
@pytest.mark.parametrize("new_input", ["none", "reserved", "steer"])
async def test_recovered_final_keeps_result_and_finishes_batch_before_new_input(
    monkeypatch: pytest.MonkeyPatch,
    ending: str,
    new_input: str,
) -> None:
    requests: list[list[ModelMessage]] = []
    channel = uuid4()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        requests.append(messages)
        if len(requests) > 1:
            yield "Handled the new input"
        elif ending == "text":
            yield "Original final text"
        else:
            yield "Original wait output"
            yield {
                0: DeltaToolCall(
                    name="wait",
                    json_args=json.dumps({"wait_for": [str(channel)]}),
                    tool_call_id="wait-original",
                ),
                1: DeltaToolCall(name="process_list", json_args="{}", tool_call_id="same-batch"),
            }

    model = FunctionModel(stream_function=stream)
    monkeypatch.setattr(runner_module, "OpenAIChatModel", lambda *args, **kwargs: model)
    caller = Caller()
    async with httpx2.AsyncClient() as client:
        agent = runner(client, caller)
        ctx = CancelAfterFinalCheckpoint(agent.initial_state(instructions="", skills=[]))
        with pytest.raises(asyncio.CancelledError):
            await agent(ctx)
        assert len(requests) == 1
        item = SessionInput(uuid4(), 2, "steer", "Please also handle this", None)
        if new_input == "reserved":
            ctx.inputs = (item,)
        elif new_input == "steer":
            ctx.steer.append(item)
        ctx.attempt, ctx.recovered = 2, True
        result = await agent(ctx)

    if new_input == "none":
        assert len(requests) == 1
        assert result.output == (
            "Original final text" if ending == "text" else "Original wait output"
        )
        assert result.wait_for == (() if ending == "text" else (channel,))
    else:
        assert len(requests) == 2
        assert "Please also handle this" in str(requests[-1])
        assert result.output == "Handled the new input"
        assert result.wait_for == ()
        assert any(item.id in write.consumed_input_ids for write in ctx.writes)
    if ending == "wait":
        returns = [
            part
            for write in ctx.writes
            for message in write.messages
            for part in cast(Any, message.data["parts"])
            if part["part_kind"] == "tool-return"
        ]
        assert {part["tool_call_id"] for part in returns} == {"wait-original", "same-batch"}
        assert len(caller.calls) <= 1
        if not caller.calls:
            assert "outcome_unknown" in str(returns)
    assert result.checkpoint.number == ctx.checkpoint_number + 1
    assert result.checkpoint not in ctx.writes
    assert UUID(cast(Any, result.checkpoint.state.data["cycles"])[-1]["turn_id"]) == ctx.run_id

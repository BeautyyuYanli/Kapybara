import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic_ai.messages import ModelMessage, RetryPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from kapy.agent import OpenAICompatibleBackend
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
    monkeypatch.setattr(OpenAICompatibleBackend, "create_model", lambda *args, **kwargs: model)
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


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", ["normal", "new_retry", "saved_retry"])
async def test_batch_retry_blocks_later_wait_but_new_response_can_finish(
    monkeypatch: pytest.MonkeyPatch,
    recovery: str,
) -> None:
    requests: list[list[ModelMessage]] = []
    first_channel, corrected_channel = uuid4(), uuid4()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        requests.append(messages)
        if len(requests) == 1:
            yield {
                0: DeltaToolCall(name="process_list", json_args='{"limit":0}', tool_call_id="bad"),
                1: DeltaToolCall(
                    name="wait",
                    json_args=json.dumps({"wait_for": [str(first_channel)]}),
                    tool_call_id="blocked-wait",
                ),
                2: DeltaToolCall(name="process_list", json_args="{}", tool_call_id="last-tool"),
            }
        else:
            assert len(requests) == 2
            yield "Corrected final output"
            yield {
                0: DeltaToolCall(
                    name="wait",
                    json_args=json.dumps({"wait_for": [str(corrected_channel)]}),
                    tool_call_id="corrected-wait",
                )
            }

    class CancelAtBatchBoundary(Context):
        interruption = 0

        async def checkpoint(self, write: CheckpointWrite) -> str:
            cursor = await super().checkpoint(write)
            current = cast(Any, write.state.data["cycles"])[-1]
            retry_saved = any(
                part["part_kind"] == "retry-prompt" and part.get("tool_call_id") == "bad"
                for message in write.messages
                for part in cast(Any, message.data["parts"])
            )
            if self.interruption == 0 and current.get("pending_final"):
                self.interruption = 1
                raise asyncio.CancelledError
            if recovery == "saved_retry" and self.interruption == 1 and retry_saved:
                self.interruption = 2
                raise asyncio.CancelledError
            return cursor

    monkeypatch.setattr(
        OpenAICompatibleBackend,
        "create_model",
        lambda *args, **kwargs: FunctionModel(stream_function=stream),
    )
    async with httpx2.AsyncClient() as client:
        agent = runner(client)
        initial = agent.initial_state(instructions="", skills=[])
        ctx = Context(initial) if recovery == "normal" else CancelAtBatchBoundary(initial)
        if recovery != "normal":
            with pytest.raises(asyncio.CancelledError):
                await agent(ctx)
            ctx.attempt, ctx.recovered = 2, True
            if recovery == "saved_retry":
                with pytest.raises(asyncio.CancelledError):
                    await agent(ctx)
                ctx.attempt = 3
        result = await agent(ctx)

    assert len(requests) == 2
    assert any(
        isinstance(part, RetryPromptPart) and part.tool_call_id == "bad"
        for message in requests[1]
        for part in message.parts
    )
    assert result.output == "Corrected final output"
    assert result.wait_for == (corrected_channel,)
    assert result.checkpoint.number == ctx.checkpoint_number + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_first", [False, True])
@pytest.mark.parametrize("recovery", ["normal", "before_tools", "after_results"])
async def test_output_retry_preserves_same_batch_successful_wait(
    monkeypatch: pytest.MonkeyPatch,
    valid_first: bool,
    recovery: str,
) -> None:
    requests: list[list[ModelMessage]] = []
    channel = uuid4()

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        requests.append(messages)
        if len(requests) > 1:
            yield "Unexpected extra inference"
            return
        yield "Chosen wait output"
        good = DeltaToolCall(
            name="wait",
            json_args=json.dumps({"wait_for": [str(channel)]}),
            tool_call_id="valid-wait",
        )
        bad = DeltaToolCall(
            name="wait", json_args='{"wait_for":["not-a-uuid"]}', tool_call_id="invalid-wait"
        )
        calls = [good, bad] if valid_first else [bad, good]
        yield dict(enumerate(calls))

    class CancelBeforeOutputTools(Context):
        interrupted = False

        async def checkpoint(self, write: CheckpointWrite) -> str:
            cursor = await super().checkpoint(write)
            if not self.interrupted and any(
                message.kind == "model_response" for message in write.messages
            ):
                self.interrupted = True
                raise asyncio.CancelledError
            return cursor

    monkeypatch.setattr(
        OpenAICompatibleBackend,
        "create_model",
        lambda *args, **kwargs: FunctionModel(stream_function=stream),
    )
    async with httpx2.AsyncClient() as client:
        agent = runner(client)
        initial = agent.initial_state(instructions="", skills=[])
        if recovery == "before_tools":
            ctx = CancelBeforeOutputTools(initial)
        elif recovery == "after_results":
            ctx = CancelAfterFinalCheckpoint(initial)
        else:
            ctx = Context(initial)
        if recovery != "normal":
            with pytest.raises(asyncio.CancelledError):
                await agent(ctx)
            ctx.attempt, ctx.recovered = 2, True
        result = await agent(ctx)

    assert len(requests) == 1
    assert result.output == "Chosen wait output"
    assert result.wait_for == (channel,)
    assert result.checkpoint.number == ctx.checkpoint_number + 1

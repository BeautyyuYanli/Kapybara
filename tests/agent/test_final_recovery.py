import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx2
import pytest
from pydantic_ai.messages import ModelMessage, RetryPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from kapy.agent import OpenAICompatibleBackend
from kapy.state import CheckpointWrite, ReplyTo, SessionInput, WaitFor

from .test_runner import Caller, Context, runner  # type: ignore[missing-import]


class CancelAfterFinalCheckpoint(Context):
    interrupted = False

    async def checkpoint(self, write: CheckpointWrite) -> str:
        cursor = await super().checkpoint(write)
        cycle = cast(Any, write.state.data["cycles"])[-1]
        if not self.interrupted and (cycle.get("pending_final") or cycle.get("output_candidates")):
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
                    name="wait_for",
                    json_args=json.dumps({"ids": [str(channel)]}),
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
        assert result.output == ("Original final text" if ending == "text" else WaitFor((channel,)))
    else:
        assert len(requests) == 2
        assert "Please also handle this" in str(requests[-1])
        assert result.output == "Handled the new input"
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
                    name="wait_for",
                    json_args=json.dumps({"ids": [str(first_channel)]}),
                    tool_call_id="blocked-wait",
                ),
                2: DeltaToolCall(name="process_list", json_args="{}", tool_call_id="last-tool"),
            }
        else:
            assert len(requests) == 2
            yield "Corrected final output"
            yield {
                0: DeltaToolCall(
                    name="wait_for",
                    json_args=json.dumps({"ids": [str(corrected_channel)]}),
                    tool_call_id="corrected-wait",
                )
            }

    class CancelAtBatchBoundary(Context):
        interruption = 0

        async def checkpoint(self, write: CheckpointWrite) -> str:
            cursor = await super().checkpoint(write)
            retry_saved = any(
                part["part_kind"] == "retry-prompt" and part.get("tool_call_id") == "bad"
                for message in write.messages
                for part in cast(Any, message.data["parts"])
            )
            if self.interruption == 0 and any(m.kind == "model_response" for m in write.messages):
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
    assert result.output == WaitFor((corrected_channel,))
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
            name="wait_for",
            json_args=json.dumps({"ids": [str(channel)]}),
            tool_call_id="valid-wait",
        )
        bad = DeltaToolCall(
            name="wait_for", json_args='{"ids":["not-a-uuid"]}', tool_call_id="invalid-wait"
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
    assert result.output == WaitFor((channel,))
    assert result.checkpoint.number == ctx.checkpoint_number + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["wait_for", "reply_to"])
@pytest.mark.parametrize("recovered", [False, True])
async def test_null_output_ids_retry_and_finish_the_batch(monkeypatch, tool_name, recovered):
    address = uuid4()
    requests = []

    async def stream(messages, info):
        requests.append(messages)
        yield "Corrected answer" if len(requests) > 1 else "First answer"
        if len(requests) == 1:
            yield {
                0: DeltaToolCall(name=tool_name, json_args='{"ids":null}', tool_call_id="invalid"),
                1: DeltaToolCall(name="process_list", json_args="{}", tool_call_id="same-batch"),
            }
        else:
            yield {
                0: DeltaToolCall(
                    name=tool_name,
                    json_args=json.dumps({"ids": [str(address)]}),
                    tool_call_id="corrected",
                )
            }

    class InterruptedResponse(Context):
        interrupted = False

        async def checkpoint(self, write):
            cursor = await super().checkpoint(write)
            if not self.interrupted and any(m.kind == "model_response" for m in write.messages):
                self.interrupted = True
                raise asyncio.CancelledError
            return cursor

    monkeypatch.setattr(
        OpenAICompatibleBackend,
        "create_model",
        lambda *args, **kwargs: FunctionModel(stream_function=stream),
    )
    caller = Caller()
    async with httpx2.AsyncClient() as client:
        agent = runner(client, caller)
        ctx = (InterruptedResponse if recovered else Context)(
            agent.initial_state(instructions="", skills=[])
        )
        if tool_name == "reply_to":
            ctx.session = replace(ctx.session, config={"output_mode": "reply_to"})
            ctx.inputs = (SessionInput(uuid4(), 1, "queue", "Question", None, address),)
        if recovered:
            with pytest.raises(asyncio.CancelledError):
                await agent(ctx)
            ctx.recovered, ctx.attempt = True, 2
        result = await agent(ctx)
    assert len(requests) == 2
    assert result.output == (
        WaitFor((address,)) if tool_name == "wait_for" else ReplyTo((address,), "Corrected answer")
    )
    assert any(
        isinstance(part, RetryPromptPart) and part.tool_call_id == "invalid"
        for message in requests[-1]
        for part in message.parts
    )
    assert any(
        part.tool_call_id == "same-batch"
        for message in requests[-1]
        for part in message.parts
        if part.part_kind == "tool-return"
    )
    assert len(caller.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["wait_for", "reply_to"])
@pytest.mark.parametrize(
    "invalid_args", ["missing", "null_ids", "extra", "array", "string", "null"]
)
@pytest.mark.parametrize("recovered", [False, True])
async def test_invalid_output_arguments_preserve_same_batch_exit(
    monkeypatch: pytest.MonkeyPatch, tool_name: str, invalid_args: str, recovered: bool
) -> None:
    address = uuid4()
    args = {
        "missing": {},
        "null_ids": {"ids": None},
        "extra": {"ids": [str(address)], "payload": "unexpected"},
        "array": [str(address)],
        "string": "unexpected",
        "null": None,
    }[invalid_args]
    valid_tool = "reply_to" if tool_name == "wait_for" else "wait_for"
    requests: list[list[ModelMessage]] = []

    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        requests.append(messages)
        assert len(requests) == 1
        yield "Chosen answer"
        yield {
            0: DeltaToolCall(name=tool_name, json_args=json.dumps(args), tool_call_id="invalid"),
            1: DeltaToolCall(
                name=valid_tool,
                json_args=json.dumps({"ids": [str(address)]}),
                tool_call_id="valid",
            ),
        }

    class InterruptedResponse(Context):
        interrupted = False

        async def checkpoint(self, write: CheckpointWrite) -> str:
            cursor = await super().checkpoint(write)
            if not self.interrupted and any(m.kind == "model_response" for m in write.messages):
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
        ctx = (InterruptedResponse if recovered else Context)(
            agent.initial_state(instructions="", skills=[])
        )
        ctx.session = replace(ctx.session, config={"output_mode": "reply_to"})
        ctx.inputs = (SessionInput(uuid4(), 1, "queue", "Question", None, address),)
        if recovered:
            with pytest.raises(asyncio.CancelledError):
                await agent(ctx)
            ctx.recovered, ctx.attempt = True, 2
        result = await agent(ctx)

    assert len(requests) == 1
    assert result.output == (
        ReplyTo((address,), "Chosen answer") if valid_tool == "reply_to" else WaitFor((address,))
    )
    assert any(
        part["part_kind"] == "retry-prompt" and part.get("tool_call_id") == "invalid"
        for write in [*ctx.writes, result.checkpoint]
        for message in write.messages
        for part in cast(Any, message.data["parts"])
    )

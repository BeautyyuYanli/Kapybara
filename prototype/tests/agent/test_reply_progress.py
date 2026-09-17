"""Partial replies commit independently while one framework run keeps working."""

import asyncio
import json
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import httpx2
import pytest
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from kapy.agent import OpenAICompatibleBackend
from kapy.state import ReplyTo, SessionInput, WaitFor

from .test_runner import Caller, Context, response, runner

pytestmark = pytest.mark.asyncio


async def test_partial_reply_returns_remaining_and_continues_same_framework_run():
    first, second = uuid4(), uuid4()
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        index = len(requests)
        if index == 2:
            tool = next(m for m in requests[-1]["messages"] if m["role"] == "tool")
            assert json.loads(tool["content"]) == {
                "output": {
                    "kind": "reply_to",
                    "being_waited_ids": [str(first)],
                    "payload": "First answer",
                },
                "remaining_being_waited_ids": [str(second)],
            }
        assert index <= 2
        return response(
            text="First answer" if index == 1 else "Second answer",
            name="reply_to",
            args={"ids": [str(first if index == 1 else second)]},
            call_id=f"reply-{index}",
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        ctx.session = replace(ctx.session, config={"output_mode": "reply_to"})
        ctx.inputs = (
            SessionInput(uuid4(), 1, "queue", "First", None, first),
            SessionInput(uuid4(), 2, "queue", "Second", None, second),
        )
        result = await agent(ctx)
    assert result.output == ReplyTo((second,), "Second answer")
    assert len(ctx.reply_receipts) == 2
    data = cast(dict[str, Any], result.checkpoint.state.data)
    assert data["run_usage"] == {"input_tokens": 200, "output_tokens": 20}
    assert len(data["cycles"]) == 1
    assert [value["payload"] for value in data["cycles"][0]["outputs"]] == [
        "First answer",
        "Second answer",
    ]


@pytest.mark.parametrize("interrupt_at", ["commit", "return", "final"])
async def test_committed_reply_replays_before_address_validation(interrupt_at):
    address = uuid4()
    calls = 0

    class Interrupted(Context):
        interrupted = False

        async def reply(self, **kwargs):
            receipt = await super().reply(**kwargs)
            if interrupt_at == "commit" and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError
            return receipt

        async def checkpoint(self, write):
            cursor = await super().checkpoint(write)
            cycle = write.state.data["cycles"][-1]
            field = "batch_replies" if interrupt_at == "return" else "pending_final"
            if interrupt_at != "commit" and cycle.get(field) and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError
            return cursor

    def handle(request):
        nonlocal calls
        calls += 1
        return response(text="Saved body", name="reply_to", args={"ids": [str(address)]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        ctx = Interrupted(agent.initial_state(instructions="", skills=[]))
        ctx.session = replace(ctx.session, config={"output_mode": "reply_to"})
        ctx.inputs = (SessionInput(uuid4(), 1, "queue", "Question", None, address),)
        with pytest.raises(asyncio.CancelledError):
            await agent(ctx)
        assert len(ctx.reply_receipts) == 1
        assert not (await ctx.unreplied_addresses()).being_waited_ids
        ctx.recovered, ctx.attempt = True, 2
        result = await agent(ctx)
    assert calls == 1
    assert result.output == ReplyTo((address,), "Saved body")
    cycle = cast(dict[str, Any], result.checkpoint.state.data)["cycles"][-1]
    assert len(cycle["outputs"]) == 1
    returns = [p for m in cycle["messages"] for p in m["parts"] if p["part_kind"] == "tool-return"]
    assert len(returns) == 1 and returns[0]["content"]["output"]["payload"] == "Saved body"


@pytest.mark.parametrize("ending", ["reply", "wait", "retry"])
async def test_reply_batch_finishes_tools_and_preserves_wait_or_retry(monkeypatch, ending):
    first, second, channel = uuid4(), uuid4(), uuid4()
    requests = []

    async def stream(messages, info):
        requests.append(messages)
        yield "Batch body"
        if len(requests) == 2:
            assert ending == "retry"
            assert any(
                isinstance(p, ToolReturnPart) and p.tool_name == "reply_to"
                for m in messages
                for p in m.parts
            )
            yield {0: DeltaToolCall(name="reply_to", json_args='{"ids":[]}', tool_call_id="empty")}
            return
        yield {
            0: DeltaToolCall(
                name="reply_to", json_args=json.dumps({"ids": [str(first)]}), tool_call_id="first"
            ),
            1: DeltaToolCall(
                name="reply_to", json_args=json.dumps({"ids": [str(second)]}), tool_call_id="second"
            ),
            2: DeltaToolCall(name="process_list", json_args="{}", tool_call_id="after-clear"),
            **(
                {
                    3: DeltaToolCall(
                        name="wait_for",
                        json_args=json.dumps({"ids": [str(channel)]}),
                        tool_call_id="wait",
                    )
                }
                if ending == "wait"
                else {}
            ),
            **(
                {
                    3: DeltaToolCall(
                        name="process_list", json_args='{"limit":null}', tool_call_id="invalid"
                    )
                }
                if ending == "retry"
                else {}
            ),
        }

    monkeypatch.setattr(
        OpenAICompatibleBackend, "create_model", lambda *a: FunctionModel(stream_function=stream)
    )
    caller = Caller()
    async with httpx2.AsyncClient() as client:
        agent = runner(client, caller)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        ctx.session = replace(ctx.session, config={"output_mode": "reply_to"})
        ctx.inputs = (
            SessionInput(uuid4(), 1, "queue", "First", None, first),
            SessionInput(uuid4(), 2, "queue", "Second", None, second),
        )
        result = await agent(ctx)
    assert len(caller.calls) == 1
    trailing_returns = [
        part
        for message in cast(Any, result.checkpoint.state.data)["cycles"][-1]["messages"]
        for part in message["parts"]
        if part["part_kind"] == "tool-return" and part["tool_call_id"] == "after-clear"
    ]
    assert len(trailing_returns) == 1
    assert trailing_returns[0]["content"] == {"items": [], "next": None}
    assert len(requests) == (2 if ending == "retry" else 1)
    assert result.output == (
        WaitFor((channel,))
        if ending == "wait"
        else ReplyTo(() if ending == "retry" else (second,), "Batch body")
    )
    assert [r.output.being_waited_ids for r in ctx.reply_receipts.values()][:2] == [
        (first,),
        (second,),
    ]


async def test_steer_requires_new_text_and_cannot_inherit_pre_input_body():
    first, second = uuid4(), uuid4()
    calls = 0
    ctx = None

    def handle(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            assert ctx is not None
            ctx.steer.append(SessionInput(uuid4(), 2, "steer", "New question", None, second))
            return response(text="First body", name="reply_to", args={"ids": [str(first)]})
        return response(
            text=None if calls == 2 else "Fresh body",
            name="reply_to",
            args={"ids": [str(second)]},
            call_id=f"new-{calls}",
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        ctx.session = replace(ctx.session, config={"output_mode": "reply_to"})
        ctx.inputs = (SessionInput(uuid4(), 1, "queue", "First", None, first),)
        result = await agent(ctx)
    assert calls == 3
    assert [r.output for r in ctx.reply_receipts.values()] == [
        ReplyTo((first,), "First body"),
        ReplyTo((second,), "Fresh body"),
    ]
    assert result.output == ReplyTo((second,), "Fresh body")


async def test_recovery_after_waiting_input_consumption_does_not_finish_with_old_reply():
    address = uuid4()
    waiting_input = SessionInput(uuid4(), 2, "steer", "Dependency result", uuid4())
    requests = []

    class Interrupted(Context):
        interrupted = False

        async def reply(self, **kwargs):
            receipt = await super().reply(**kwargs)
            if receipt.output.being_waited_ids:
                self.steer.append(waiting_input)
            return receipt

        async def checkpoint(self, write):
            cursor = await super().checkpoint(write)
            if waiting_input.id in write.consumed_input_ids and not self.interrupted:
                self.interrupted = True
                raise asyncio.CancelledError
            return cursor

    def handle(request):
        requests.append(json.loads(request.content))
        return response(
            text="Initial answer" if len(requests) == 1 else "Handled dependency result",
            name="reply_to",
            args={"ids": [str(address)] if len(requests) == 1 else []},
            call_id=f"reply-{len(requests)}",
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        ctx = Interrupted(agent.initial_state(instructions="", skills=[]))
        ctx.session = replace(ctx.session, config={"output_mode": "reply_to"})
        ctx.inputs = (SessionInput(uuid4(), 1, "queue", "Question", None, address),)
        with pytest.raises(asyncio.CancelledError):
            await agent(ctx)
        assert len(requests) == 1
        ctx.recovered, ctx.attempt = True, 2
        result = await agent(ctx)
    assert len(requests) == 2
    assert "Dependency result" in json.dumps(requests[-1]["messages"])
    assert result.output == ReplyTo((), "Handled dependency result")
    assert [
        value["payload"]
        for value in cast(Any, result.checkpoint.state.data)["cycles"][-1]["outputs"]
    ] == ["Initial answer", "Handled dependency result"]

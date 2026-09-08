"""The model emits addresses; Pydantic AI returns the complete output DTO."""

import json
from dataclasses import replace
from uuid import uuid4

import httpx2
import pytest

from kapy.state import ReplyTo, SessionInput, WaitFor

from .test_runner import Context, response, runner


@pytest.mark.asyncio
async def test_reply_schema_prompt_and_complete_framework_output() -> None:
    address = uuid4()
    requests = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return response(text="The complete answer", name="reply_to", args={"ids": [str(address)]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        ctx = Context(agent.initial_state(instructions="Business instructions", skills=[]))
        ctx.session = replace(ctx.session, config={"output_mode": "reply_to"})
        ctx.inputs = (SessionInput(uuid4(), 1, "queue", "Question", None, address),)
        result = await agent(ctx)
    assert result.output == ReplyTo((address,), "The complete answer")
    tools = {tool["function"]["name"]: tool["function"] for tool in requests[0]["tools"]}
    assert {"wait_for", "reply_to"} <= tools.keys()
    assert set(tools["reply_to"]["parameters"]["properties"]) == {"ids"}
    assert "being_waited_id" in json.dumps(requests[0]["messages"])
    assert requests[0]["tool_choice"] == "required"
    cycles = result.checkpoint.state.data["cycles"]
    assert isinstance(cycles, list)
    cycle = cycles[-1]
    assert isinstance(cycle, dict)
    outputs = cycle["outputs"]
    assert outputs == [
        {
            "kind": "reply_to",
            "being_waited_ids": [str(address)],
            "payload": "The complete answer",
        }
    ]
    assert isinstance(outputs, list)
    pending = cycle["pending_final"]
    assert isinstance(pending, dict)
    assert pending["output"] == outputs[-1]


@pytest.mark.asyncio
async def test_text_mode_hides_reply_protocol_and_keeps_raw_input() -> None:
    address = uuid4()
    requests = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        return response(text="Answer")

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        ctx.inputs = (SessionInput(uuid4(), 1, "queue", "Question", None, address),)
        result = await agent(ctx)
    assert result.output == "Answer"
    assert "reply_to" not in json.dumps(requests)
    assert "being_waited_id" not in json.dumps(requests)
    assert str(address) not in json.dumps(requests)
    assert any(message.get("content") == "Question" for message in requests[0]["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["empty", "foreign", "duplicate", "no_text"])
async def test_invalid_reply_is_corrected_by_model(invalid: str) -> None:
    address = uuid4()
    requests = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if len(requests) == 1:
            ids = (
                []
                if invalid == "empty"
                else [str(uuid4())]
                if invalid == "foreign"
                else [str(address)] * (2 if invalid == "duplicate" else 1)
            )
            return response(
                text=None if invalid == "no_text" else "Premature",
                name="reply_to",
                args={"ids": ids},
            )
        return response(
            text="Corrected", name="reply_to", args={"ids": [str(address)]}, call_id="correct"
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        ctx.session = replace(ctx.session, config={"output_mode": "reply_to"})
        ctx.inputs = (SessionInput(uuid4(), 1, "queue", "Question", None, address),)
        result = await agent(ctx)
    assert len(requests) == 2
    assert result.output == ReplyTo((address,), "Corrected")


@pytest.mark.asyncio
async def test_text_alone_does_not_end_explicit_mode_but_is_reply_body() -> None:
    address = uuid4()
    calls = 0

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return response(text="Complete body in previous response")
        return response(name="reply_to", args={"ids": [str(address)]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        ctx.session = replace(ctx.session, config={"output_mode": "reply_to"})
        ctx.inputs = (SessionInput(uuid4(), 1, "queue", "Question", None, address),)
        result = await agent(ctx)
    assert calls == 2
    assert result.output == ReplyTo((address,), "Complete body in previous response")


@pytest.mark.asyncio
async def test_wait_rejects_empty_ids() -> None:
    channel = uuid4()
    calls = 0

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return response(name="wait_for", args={"ids": [] if calls == 1 else [str(channel)]})

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        result = await agent(Context(agent.initial_state(instructions="", skills=[])))
    assert calls == 2
    assert result.output == WaitFor((channel,))

import asyncio
import copy
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any
from uuid import uuid4

import httpx2
import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

from kapy.agent import AgentResourceLimit
from kapy.agent import runner as runner_module
from kapy.skills import SkillDescription
from kapy.state import JsonValue, SessionInput

from .test_runner import Caller, Context, response, runner  # type: ignore[missing-import]


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", ["incomplete", "oversized"])
async def test_unfinished_or_oversized_tool_arguments_never_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    arguments: str,
) -> None:
    async def stream(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator[Any]:
        yield {
            0: DeltaToolCall(
                name="apply_patch",
                tool_call_id="never-dispatch",
                json_args='{"patch":'
                if arguments == "incomplete"
                else json.dumps({"patch": "x" * (256 * 1024)}),
            )
        }
        if arguments == "incomplete":
            raise asyncio.CancelledError

    model = FunctionModel(stream_function=stream)
    monkeypatch.setattr(runner_module, "OpenAIChatModel", lambda *a, **kw: model)
    caller = Caller()
    async with httpx2.AsyncClient() as client:
        agent = runner(client, caller)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        error = asyncio.CancelledError if arguments == "incomplete" else AgentResourceLimit
        with pytest.raises(error):
            await agent(ctx)
    assert caller.calls == []


@pytest.mark.asyncio
async def test_one_runner_overlapping_sessions_keep_creation_snapshots() -> None:
    barrier = asyncio.Barrier(2)
    channels = {name: uuid4() for name in ("alice", "bob")}
    requests = {}
    authorized = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        data = json.loads(request.content)
        name = next(n for n in channels if f"input-{n}" in json.dumps(data["messages"]))
        requests[name] = data
        await asyncio.wait_for(barrier.wait(), 2)
        return response(
            text=f"output-{name}",
            name="wait",
            args={"wait_for": [str(channels[name])]},
            call_id=f"wait-{name}",
        )

    async def authorize(session_id, wait_for):
        authorized.append((session_id, wait_for))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        agent = runner(client)
        agent.authorize_wait = authorize
        contexts = {}
        for name in channels:
            skills = [SkillDescription(str(uuid4()), f"catalog-{name}")]
            ctx = Context(agent.initial_state(instructions=f"instruction-{name}", skills=skills))
            skills[:] = [SkillDescription(str(uuid4()), "changed-after-creation")]
            ctx.inputs = (SessionInput(uuid4(), 1, "queue", f"input-{name}", None),)
            contexts[name] = ctx
        results = await asyncio.gather(*(agent(ctx) for ctx in contexts.values()))
    for (name, ctx), result in zip(contexts.items(), results, strict=True):
        body = json.dumps(requests[name]["messages"])
        other = "bob" if name == "alice" else "alice"
        assert f"instruction-{name}" in body and f"catalog-{name}" in body
        assert f"instruction-{other}" not in body and f"catalog-{other}" not in body
        assert "changed-after-creation" not in body and f"input-{other}" not in body
        assert result.output == f"output-{name}" and result.wait_for == (channels[name],)
        assert (ctx.session.id, (channels[name],)) in authorized
        assert {i for w in ctx.writes for i in w.consumed_input_ids} == {ctx.inputs[0].id}
        cycles = result.checkpoint.state.data["cycles"]
        assert isinstance(cycles, list) and cycles
        for cycle in cycles:
            assert isinstance(cycle, dict)
            assert cycle["turn_id"] == str(ctx.run_id)
        archived = json.dumps([m.data for w in ctx.writes for m in w.messages])
        assert f"input-{name}" in archived and f"input-{other}" not in archived
        assert f"wait-{name}" in archived and f"wait-{other}" not in archived
    assert len(authorized) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fresh_tokens", [300, 800])
async def test_provider_usage_controls_next_projection_once_across_restart(
    fresh_tokens: int,
) -> None:
    requests = []

    class MarkedCaller(Caller):
        async def call(self, *args, **kwargs):
            await super().call(*args, **kwargs)
            return {"items": [], "marker": "original-result-evidence", "next": None}

    def handle(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        match len(requests):
            case 1:
                return response(name="process_list", call_id="old-call", tokens=300)
            case 2:
                return response(text="old final", tokens=300)
            case 3:
                return response(name="process_list", call_id="new-call", tokens=fresh_tokens)
            case 4:
                raise asyncio.CancelledError
            case _:
                return response(text="restored final", tokens=300)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
        caller = MarkedCaller()
        agent = runner(client, caller)
        agent.config = replace(agent.config, context_window_tokens=1000, max_output_tokens=100)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        first = await agent(ctx)
        await ctx.checkpoint(first.checkpoint)  # State commits the returned final checkpoint.
        ctx.run_id = uuid4()
        ctx.session = replace(ctx.session, run_id=ctx.run_id)
        ctx.inputs = (SessionInput(uuid4(), 2, "queue", "latest-input-survives", None),)
        with pytest.raises(asyncio.CancelledError):
            await agent(ctx)
        saved = copy.deepcopy(ctx.state)
        level = 1 if fresh_tokens == 800 else 0
        assert saved.data["cycles"][0]["level"] == level
        assert saved.data["last_usage"]["input_tokens"] == fresh_tokens
        assert saved.data["last_usage"]["sweep_applied"] is (fresh_tokens == 800)
        restarted = runner(client, caller)
        restarted.config = agent.config
        ctx.attempt, ctx.recovered = 2, True
        result = await restarted(ctx)
    assert result.output == "restored final"
    assert requests[3]["messages"] == requests[4]["messages"]
    cycles = result.checkpoint.state.data["cycles"]
    assert isinstance(cycles, list) and cycles
    first_cycle = cycles[0]
    assert isinstance(first_cycle, dict)
    assert first_cycle["level"] == level
    for projected in requests[3:]:
        assert "latest-input-survives" in json.dumps(projected["messages"])
        old = next(m for m in projected["messages"] if m.get("tool_call_id") == "old-call")
        assert ("original-result-evidence" in old["content"]) is (fresh_tokens == 300)
    archived = [m.data for w in ctx.writes for m in w.messages]
    parts: list[dict[str, JsonValue]] = []
    for message in archived:
        message_parts = message["parts"]
        assert isinstance(message_parts, list)
        for part in message_parts:
            assert isinstance(part, dict)
            parts.append(part)
    original = next(
        p
        for p in parts
        if p.get("tool_call_id") == "old-call" and p["part_kind"] == "tool-return"
    )
    assert "original-result-evidence" in json.dumps(original["content"])

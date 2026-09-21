"""Generic pages and execution hooks exercise the real SDK and PostgreSQL fences."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic_ai import Agent, CallToolsNode, ModelRequestNode
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_graph import End

from kapy.agent_runner import (
    ContextPolicy,
    full_history_policy,
    open_runner,
    summary_context_policy,
)
from kapy.agent_runner.context import ContextAssemblyContext, JsonObject, PageTurnContext
from kapy.agent_runner.repository import AgentRepository
from kapy.control.models import ModelService, UpdateModel
from kapy.control.sessions import SessionService, UpdateSession

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


async def history(database, session_id):
    async with database.sessions.begin() as db:
        return await AgentRepository(db).read_history_entries(session_id)


async def test_custom_page_protects_pending_tool_pair_and_preserves_live_request(database):
    session_id = uuid4()
    received, actions, instructions = [], [], []

    def model(messages, info):
        received.append(deepcopy(messages))
        if len(received) == 1:
            return ModelResponse(parts=[ToolCallPart("work", {}, "call")])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model))

    @agent.instructions
    def dynamic():
        instructions.append(True)
        return f"instructions {len(instructions)}"

    @agent.tool_plain
    def work() -> str:
        return "result"

    async def action(context: PageTurnContext) -> JsonObject:
        actions.append(context)
        assert context.anchor_seq == 2
        assert context.operation_id == f"{session_id}:memory/v1:2"
        assert [seq for seq, _ in await context.read_history(through_seq=999)] == [0, 1, 2]
        return {"note": "remember"}

    async def assemble(context: ContextAssemblyContext) -> list[ModelMessage]:
        if context.page is None:
            return [message for _, message in await context.read_history()]
        assert context.prefix_through_seq == 0
        assert [seq for seq, _ in await context.read_history(through_seq=999)] == [0]
        assert await context.read_history(start_seq=1) == []
        return [ModelRequest(parts=[UserPromptPart("remember")])]

    policy = ContextPolicy("memory/v1", lambda boundary: False, action, assemble)
    async with open_runner(
        session_id,
        agent=agent,
        session_factory=database.sessions,
        context_policy=policy,
    ) as runner:
        await runner.rebuild_context()
        assert not (await runner.turn(steer=["original"])).finished
        before = await history(database, session_id)
        page = await runner.turn_context_page()
        assert page is not None and page.payload == {"note": "remember"}
        # Returned payload mutation cannot mutate the runner's durable view.
        page.payload["note"] = "changed by caller"
        reused = await runner.turn_context_page()
        assert reused is not None and reused.payload == {"note": "remember"}
        assert len(actions) == 1 and await history(database, session_id) == before
        assert (await runner.turn(steer=["new steer"])).output == "done"
    assert [p.content for m in received[-1] for p in m.parts if isinstance(p, UserPromptPart)] == [
        "remember",
        "new steer",
    ]
    assert [
        p.tool_call_id for m in received[-1] for p in m.parts if isinstance(p, ToolCallPart)
    ] == ["call"]
    assert [
        p.tool_call_id for m in received[-1] for p in m.parts if isinstance(p, ToolReturnPart)
    ] == ["call"]
    assert received[-1][-1].instructions == f"instructions {len(instructions)}"
    after = await history(database, session_id)
    assert after[: len(before)] == before
    assert [entry.seq for entry in after] == list(range(5))


async def test_committed_page_survives_assembly_failure_and_restarts_without_action(
    database,
    seed_history,
):
    session_id = await seed_history(
        [
            ModelRequest(parts=[UserPromptPart("old")]),
            ModelResponse(parts=[TextPart("done")]),
        ]
    )
    calls = []
    fail = True

    async def action(context: PageTurnContext) -> JsonObject:
        calls.append(context.operation_id)
        return {"artifact": "saved"}

    async def assemble(context: ContextAssemblyContext) -> list[ModelMessage]:
        if context.page is None:
            return [message for _, message in await context.read_history()]
        if fail:
            raise RuntimeError("assembly failed")
        return [ModelRequest(parts=[UserPromptPart(str(context.page.payload["artifact"]))])]

    policy = ContextPolicy("artifact/v1", lambda boundary: False, action, assemble)
    with pytest.raises(RuntimeError, match="assembly failed"):
        async with open_runner(
            session_id,
            agent=Agent("test"),
            session_factory=database.sessions,
            context_policy=policy,
        ) as runner:
            await runner.rebuild_context()
            await runner.turn_context_page()
    fail = False
    async with open_runner(
        session_id,
        agent=Agent("test"),
        session_factory=database.sessions,
        context_policy=policy,
    ) as runner:
        await runner.rebuild_context()
        page = await runner.turn_context_page()
        assert page is not None and page.payload == {"artifact": "saved"}
        assert (await runner.turn()).finished
    assert len(calls) == 1 and len(await history(database, session_id)) == 2
    with pytest.raises(ValueError, match="does not match"):
        async with open_runner(
            session_id,
            agent=Agent("test"),
            session_factory=database.sessions,
        ) as runner:
            await runner.rebuild_context()


async def test_full_history_policy_manual_page_has_no_action_or_extra_model_request(
    database,
    seed_history,
):
    original = [ModelRequest(parts=[UserPromptPart("pending")])]
    session_id = await seed_history(original, "model_request")
    received = []

    def model(messages, info):
        received.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("done")])

    async with open_runner(
        session_id,
        agent=Agent(FunctionModel(model)),
        session_factory=database.sessions,
    ) as runner:
        await runner.rebuild_context()
        page = await runner.turn_context_page()
        assert page is not None and page.payload == {} and page.policy_key == "history/v1"
        assert received == []
        assert (await runner.turn()).output == "done"
    assert len(received) == 1 and received[0][0].parts == original[0].parts


@pytest.mark.parametrize("outermost", [False, True])
async def test_business_after_hooks_precede_fenced_checkpoint_and_tools(database, outermost):
    session_id = uuid4()
    observations = []

    class BusinessHook(AbstractCapability):
        def get_ordering(self):
            return CapabilityOrdering(position="outermost") if outermost else None

        async def after_node_run(self, ctx, *, node, result):
            if isinstance(node, ModelRequestNode):
                observations.append(len(await history(database, session_id)))
                assert isinstance(result, CallToolsNode)
                result.model_response.parts = [*result.model_response.parts, TextPart("hook note")]
            if isinstance(node, CallToolsNode) and isinstance(result, End):
                from pydantic_ai.result import FinalResult

                assert isinstance(result.data, FinalResult)
                result.data.output = "hook output"
            return result

    calls = 0

    def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("work", {}, "call")])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model), capabilities=[BusinessHook()])

    @agent.tool_plain
    async def work() -> str:
        saved = await history(database, session_id)
        assert len(saved) == 2 and saved[-1].message.parts[-1] == TextPart("hook note")
        return "result"

    async with open_runner(session_id, agent=agent, session_factory=database.sessions) as runner:
        await runner.rebuild_context()
        assert not (await runner.turn(steer=["go"])).finished
        result = await runner.turn()
        assert result.finished and result.output == "hook output"
    assert observations == [1, 3]
    saved = await history(database, session_id)
    assert saved[-1].message.parts[-1] == TextPart("hook note")


async def test_page_restore_preserves_earlier_completed_call_when_pending_call_reuses_id(
    database,
    seed_history,
):
    session_id = await seed_history(
        [
            ModelRequest(parts=[UserPromptPart("work twice")]),
            ModelResponse(parts=[ToolCallPart("work", {}, "shared-id")], provider_name="test"),
            ModelRequest(parts=[ToolReturnPart("work", "old result", "shared-id")]),
            ModelResponse(parts=[ToolCallPart("work", {}, "shared-id")], provider_name="test"),
        ],
        "handle_response",
        compaction_seq=2,
    )
    original = await history(database, session_id)
    received, tools = [], []

    def model(messages, info):
        received.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model))

    @agent.tool_plain
    def work() -> str:
        tools.append(True)
        return "new result"

    async with open_runner(
        session_id,
        agent=agent,
        session_factory=database.sessions,
        context_policy=summary_context_policy(agent, replay_turns=0),
    ) as runner:
        await runner.rebuild_context()
        assert not (await runner.turn()).finished
        assert received == [] and tools == [True]
        assert (await runner.turn()).output == "done"
    replies = [
        part.content
        for message in received[0]
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert replies == ["old result", "new result"]
    saved = await history(database, session_id)
    assert saved[: len(original)] == original
    assert [entry.seq for entry in saved] == list(range(6))


async def test_custom_service_factory_bypasses_summary_fields_and_is_frozen_across_queued_runs(
    database,
    seed_session,
    session_model,
):
    session_id = uuid4()
    await seed_session(session_id)
    factories = []

    def factory(session, model, agent):
        factories.append((session.id, model.context_window, agent))
        return full_history_policy()

    sessions = SessionService(database.sessions, context_policy_factory=factory)
    await sessions.update_session(session_id, UpdateSession(compaction_threshold_tokens=None))
    session = await sessions.get_session(session_id)
    await ModelService(database.sessions).update_model(
        session.provider_id, session.model_name, UpdateModel(context_window=None)
    )
    calls = []

    async def model(messages, info):
        calls.append(True)
        if len(calls) == 1:
            await sessions.enqueue_input(session_id, "queued", "next")
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model))
    session_model(agent.model)
    await sessions.enqueue_input(session_id, "steer", "go")
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished and result.output == "done"
    assert len(calls) == 2 and len(factories) == 1
    assert factories[0][1] is None


@pytest.mark.parametrize("payload", [[], {"value": float("nan")}, {"value": object()}])
async def test_invalid_page_payload_never_commits(database, seed_history, payload):
    session_id = await seed_history(
        [
            ModelRequest(parts=[UserPromptPart("old")]),
            ModelResponse(parts=[TextPart("done")]),
        ]
    )

    async def invalid(context: PageTurnContext) -> JsonObject:
        return payload

    base = full_history_policy()
    policy = ContextPolicy("invalid/v1", lambda boundary: False, invalid, base.assemble)
    with pytest.raises(ValueError):
        async with open_runner(
            session_id,
            agent=Agent("test"),
            session_factory=database.sessions,
            context_policy=policy,
        ) as runner:
            await runner.rebuild_context()
            await runner.turn_context_page()
    async with database.sessions.begin() as db:
        assert await AgentRepository(db).read_latest_page(session_id) is None


@pytest.mark.parametrize("mode", ["skip_model", "rewrite_history", "recover_failure"])
async def test_business_hooks_cannot_bypass_durable_execution(database, seed_history, mode):
    session_id = await seed_history(
        [
            ModelRequest(parts=[UserPromptPart("original")]),
            ModelResponse(parts=[TextPart("saved")]),
        ]
    )
    original = await history(database, session_id)

    class InvalidHook(AbstractCapability):
        async def before_node_run(self, ctx, *, node):
            if mode == "skip_model" and isinstance(node, ModelRequestNode):
                return CallToolsNode(ModelResponse(parts=[TextPart("invented")]))
            return node

        async def before_model_request(self, ctx, request_context):
            if mode == "rewrite_history":
                request_context.messages[0].parts = [UserPromptPart("rewritten")]
            return request_context

        async def on_node_run_error(self, ctx, *, node, error):
            if mode == "recover_failure":
                return CallToolsNode(ModelResponse(parts=[TextPart("recovered")]))
            raise error

    def model(messages, info):
        if mode == "recover_failure":
            raise RuntimeError("model failed")
        return ModelResponse(parts=[TextPart("new response")])

    agent = Agent(FunctionModel(model), capabilities=[InvalidHook()])
    with pytest.raises(RuntimeError):
        async with open_runner(
            session_id,
            agent=agent,
            session_factory=database.sessions,
        ) as runner:
            await runner.rebuild_context()
            await runner.turn(steer=["new input"])
    saved = await history(database, session_id)
    assert saved[:2] == original
    assert len(saved) == 3
    part = saved[-1].message.parts[0]
    assert isinstance(part, UserPromptPart) and part.content == "new input"

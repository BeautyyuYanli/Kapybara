"""Generic pages and execution hooks exercise the real SDK and PostgreSQL fences."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic_ai import Agent, CallToolsNode, ModelRequestNode, RunContext
from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_graph import End

from kapy.agent_runner import ContextPage, open_runner
from kapy.agent_runner.context import ContextInput, PageInput
from kapy.agent_runner.repository import AgentRepository
from kapy.context_plugins import ContextPluginRegistry, ContextPluginSpec, SummaryPlugin
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

    class MemoryPlugin:
        key = "memory/v1"

        async def on_page(self, page: PageInput, *, call_agent) -> ContextPage:
            actions.append(page)
            assert len(page.messages) == 1
            assert [p.content for p in page.messages[0].parts if isinstance(p, UserPromptPart)] == [
                "original"
            ]
            return ContextPage({"note": "remember"})

        async def get_context(self, context: ContextInput) -> list[ModelMessage]:
            assert len(context.messages) == 1
            return [ModelRequest(parts=[UserPromptPart("remember")])]

    plugin = MemoryPlugin()
    async with open_runner(
        session_id,
        agent=agent,
        session_factory=database.sessions,
        context_plugin=plugin,
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

    class ArtifactPlugin:
        key = "artifact/v1"

        async def on_page(self, page: PageInput, *, call_agent) -> ContextPage:
            calls.append(page)
            return ContextPage({"artifact": "saved"})

        async def get_context(self, context: ContextInput) -> list[ModelMessage]:
            if fail:
                raise RuntimeError("assembly failed")
            return [ModelRequest(parts=[UserPromptPart(str(context.page.payload["artifact"]))])]

    plugin = ArtifactPlugin()
    with pytest.raises(RuntimeError, match="assembly failed"):
        async with open_runner(
            session_id,
            agent=Agent("test"),
            session_factory=database.sessions,
            context_plugin=plugin,
        ) as runner:
            await runner.rebuild_context()
            await runner.turn_context_page()
    fail = False
    async with open_runner(
        session_id,
        agent=Agent("test"),
        session_factory=database.sessions,
        context_plugin=plugin,
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


async def test_no_context_plugin_retains_history_without_creating_a_page(
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
        assert page is None
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
        context_plugin=SummaryPlugin(),
        compaction_replay_turns=0,
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


async def test_context_plugin_factory_config_is_frozen_across_queued_runs(
    database,
    seed_session,
    session_model,
):
    session_id = uuid4()
    await seed_session(session_id)
    factories = []

    def factory(config):
        factories.append(config)
        return SummaryPlugin()

    sessions = SessionService(
        database.sessions, context_plugin_registry=ContextPluginRegistry({"kapy/summary": factory})
    )
    calls = []

    async def model(messages, info):
        calls.append(True)
        if len(calls) == 1:
            await sessions.update_session(
                session_id,
                UpdateSession(context_plugin=ContextPluginSpec(config={"changed": True})),
            )
            await sessions.enqueue_input(session_id, "queued", "next")
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(FunctionModel(model))
    session_model(agent.model)
    await sessions.enqueue_input(session_id, "steer", "go")
    result = await sessions.start_runner(session_id, agent=agent)
    assert result.finished and result.output == "done"
    assert len(calls) == 2 and factories == [{}]
    await sessions.start_runner(session_id, agent=agent)
    assert factories == [{}, {"changed": True}]


@pytest.mark.parametrize("payload", [[], {"value": float("nan")}, {"value": object()}])
async def test_invalid_page_payload_never_commits(database, seed_history, payload):
    session_id = await seed_history(
        [
            ModelRequest(parts=[UserPromptPart("old")]),
            ModelResponse(parts=[TextPart("done")]),
        ]
    )

    class InvalidPlugin:
        key = "invalid/v1"

        async def on_page(self, page, *, call_agent):
            return ContextPage(payload)

        async def get_context(self, context: ContextInput) -> list[ModelMessage]:
            return []

    plugin = InvalidPlugin()
    with pytest.raises(ValueError):
        async with open_runner(
            session_id,
            agent=Agent("test"),
            session_factory=database.sessions,
            context_plugin=plugin,
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


async def test_plugin_sees_closed_page_and_reference_only_and_controls_upper_context(
    database,
    seed_history,
):
    messages = []
    for i in range(3):
        messages.extend(
            [
                ModelRequest(parts=[UserPromptPart(f"question {i}")]),
                ModelResponse(parts=[TextPart(f"answer {i}")]),
            ]
        )
    session_id = await seed_history(messages)
    pages, contexts = [], []

    class IndexPlugin:
        key = "index/v1"

        async def on_page(self, page, *, call_agent):
            pages.append(deepcopy(page))
            return ContextPage({"index": len(pages)})

        async def get_context(self, context: ContextInput) -> list[ModelMessage]:
            contexts.append(deepcopy(context))
            # Deliberately omit the reference originals. The host must not append them.
            return [ModelRequest(parts=[UserPromptPart("indexed upper context")])]

    received = []

    def model(messages, info):
        received.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("new answer")])

    async with open_runner(
        session_id,
        agent=Agent(FunctionModel(model)),
        session_factory=database.sessions,
        context_plugin=IndexPlugin(),
        compaction_replay_turns=1,
    ) as runner:
        await runner.rebuild_context()
        first = await runner.turn_context_page()
        assert first is not None and first.anchor_seq == 5
        assert len(pages[0].messages) == 6 and pages[0].previous_page is None
        assert len(contexts[-1].messages) == 2
        await runner.turn(steer=["current page"])
        second = await runner.turn_context_page()
        assert second is not None and second.anchor_seq == 7
        assert pages[1].previous_page == ContextPage({"index": 1})
        assert len(pages[1].messages) == 2
    assert [p.content for m in received[0] for p in m.parts if isinstance(p, UserPromptPart)] == [
        "indexed upper context",
        "current page",
    ]
    assert len(await history(database, session_id)) == 8


async def test_page_auxiliary_borrows_execution_capabilities_and_does_not_persist(
    database,
    seed_history,
):
    from contextlib import asynccontextmanager

    from pydantic_ai.toolsets import FunctionToolset

    from kapy.agent_runner import RunnerExecution

    prompt_baselines = []

    def dynamic(ctx: RunContext) -> str:
        prompt_baselines.append(
            [part.content for part in ctx.messages[0].parts if isinstance(part, SystemPromptPart)]
        )
        return "auxiliary system"

    session_id = await seed_history(
        [
            ModelRequest(
                parts=[
                    SystemPromptPart("host system", dynamic_ref=dynamic.__qualname__),
                    UserPromptPart("original"),
                ]
            ),
            ModelResponse(parts=[TextPart("business answer")]),
        ]
    )
    observations, helpers, tool_calls = [], [], []
    toolset = FunctionToolset()

    @toolset.tool_plain
    def work() -> str:
        tool_calls.append(True)
        return "worked"

    class StableCapability(AbstractCapability):
        def get_instructions(self):
            return "execution instruction"

        def get_toolset(self):
            return toolset

        async def before_model_request(self, ctx, request_context):
            observations.append(deepcopy(request_context.model_request_parameters))
            return request_context

    def model(messages, info):
        if len(observations) == 1:
            return ModelResponse(parts=[ToolCallPart("work", {}, "allowed")])
        return ModelResponse(parts=[TextPart("auxiliary answer")])

    agent = Agent(FunctionModel(model), instructions="fixed")
    agent.system_prompt(dynamic=True)(dynamic)

    class BorrowPlugin:
        key = "borrow/v1"

        async def on_page(self, page, *, call_agent):
            helpers.append(call_agent)
            assert await call_agent("first auxiliary") == "auxiliary answer"
            assert (
                await call_agent("second auxiliary", block_other_tools=True) == "auxiliary answer"
            )
            assert len(await history(database, session_id)) == 2
            return ContextPage({"note": "saved"})

        async def get_context(self, context: ContextInput) -> list[ModelMessage]:
            calls_before = len(observations)
            with pytest.raises(RuntimeError, match="only valid"):
                await helpers[-1]("outside on_page during assembly")
            assert len(observations) == calls_before
            return [ModelRequest(parts=[UserPromptPart("upper")])]

    @asynccontextmanager
    async def factory():
        yield RunnerExecution(
            agent, context_plugin=BorrowPlugin(), capabilities=[StableCapability()]
        )

    async with open_runner(
        session_id,
        execution_factory=factory,
        session_factory=database.sessions,
    ) as runner:
        await runner.rebuild_context()
        await runner.turn_context_page()
        with pytest.raises(RuntimeError, match="only valid"):
            await helpers[0]("too late")
    assert tool_calls == [True]
    # Each helper invocation must start from the same host-owned nested messages;
    # the first run's real SDK reevaluation must not leak into the next run.
    assert prompt_baselines == [["host system"], ["host system"]]
    # SDK provenance IDs are regenerated per run and never sent as tool definitions.
    for parameters in observations:
        for tool in parameters.function_tools:
            tool.capability_id = None
    assert observations[0] == observations[1] == observations[2]
    assert len(await history(database, session_id)) == 2


@pytest.mark.parametrize(
    "spec",
    [
        ContextPluginSpec(name="missing/plugin"),
        ContextPluginSpec(config={"invalid": True}),
    ],
)
async def test_invalid_context_plugin_fails_before_input_consumption(
    database,
    seed_session,
    session_model,
    spec,
):
    session_id = uuid4()
    await seed_session(session_id)
    sessions = SessionService(database.sessions)
    # Name is fixed on creation; this fixture writes the initial stored selection.
    from kapy.control.sessions.models import SessionRow

    async with database.sessions.begin() as db:
        row = await db.get(SessionRow, session_id)
        assert row is not None
        row.context_plugin = spec.model_dump()
    await sessions.enqueue_input(session_id, "queued", "retained")
    agent = Agent("test")
    session_model(agent.model)
    with pytest.raises(ValueError):
        await sessions.start_runner(session_id, agent=agent)
    assert [row.content for row in await sessions.read_inputs(session_id, "queued")] == ["retained"]
    assert await history(database, session_id) == ()

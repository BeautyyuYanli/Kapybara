"""Plugin contracts against real PostgreSQL and native deterministic SDK execution."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import Agent, ModelRequestNode
from pydantic_ai.capabilities import AbstractCapability, Capability, PrefixTools
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.tools import Tool
from pydantic_ai.toolsets import PrefixedToolset, RenamedToolset
from pydantic_ai.usage import RequestUsage
from sqlalchemy import func, select

from kapy.agent_plugins import (
    AgentPlugin,
    AgentPluginService,
    PluginBinding,
    PluginData,
    PluginDefinition,
    PluginOperationError,
    PluginRegistry,
    PluginSpec,
    PluginTool,
    StateConflict,
)
from kapy.agent_plugins.models import PluginBindingRow
from kapy.agent_runner import RunnerExecution, open_runner
from kapy.application.agent import create_execution_factory
from kapy.context_plugins.summary import COMPACTION_PROMPT
from kapy.control.sessions import CreateSession, SessionService, UpdateSession
from kapy.control.sessions.models import SessionRow
from kapy.interfaces.http import create_session_router
from kapy.lifecycle import LifecycleError, LifecycleStatus
from kapy.session_lease import is_session_busy

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


class Config(BaseModel):
    label: str = "test"


class State(BaseModel):
    resources: list[str] = Field(default_factory=list)


@pytest.fixture
def plugin_setup(database):
    contexts, events = [], []
    controls = {"fail_close": None, "open_error": False, "capabilities": lambda ctx: ()}

    class Plugin(AgentPlugin[Config, State]):
        @asynccontextmanager
        async def open_execution(self, ctx):
            contexts.append(ctx)
            events.append((ctx.plugin_name, "enter"))
            try:
                if controls["open_error"]:
                    raise RuntimeError("cannot open")

                async def remember(value: str) -> str:
                    previous = await ctx.state.read()
                    state = previous.value or State()
                    state.resources.append(value)
                    await ctx.state.replace(state, expected_revision=previous.revision)
                    return value

                async def instructions():
                    return f"Use {ctx.config.label} memory."

                yield PluginBinding(
                    instructions,
                    (PluginTool("remember", "Save a value", remember),),
                    controls["capabilities"](ctx),
                )
            finally:
                events.append((ctx.plugin_name, "exit"))

        async def close_session(self, ctx):
            contexts.append(ctx)
            events.append((ctx.plugin_name, "close"))
            if controls["fail_close"] == ctx.plugin_name:
                raise RuntimeError("external delete failed")
            previous = await ctx.state.read()
            await ctx.state.replace(State(), expected_revision=previous.revision)

    registry = PluginRegistry()
    definition = PluginDefinition("acme", "memory", 1, Config, State, Plugin)
    registry.register(definition)
    plugins = AgentPluginService(database.sessions, registry)
    service = SessionService(
        database.sessions,
        plugin_service=plugins,
        execution_factory=create_execution_factory(plugins),
    )
    return service, plugins, registry, definition, contexts, events, controls


def spec(name="memory", provider="acme"):
    return PluginSpec(plugin_provider=provider, plugin_name=name, config={"label": name})


async def create(service, *specs):
    return await service.create_session(
        CreateSession(provider_id=uuid4(), model_name="missing", plugins=list(specs))
    )


async def test_creation_is_atomic_ready_without_lifecycle_callbacks(database, plugin_setup):
    service, plugins, _, _, contexts, events, _ = plugin_setup
    session = await create(service, spec())
    assert session.status == LifecycleStatus.READY
    assert session.compaction_threshold_tokens == 256 * 1024 * 7 // 10
    assert not contexts and not events
    (binding,) = await plugins.list_bindings(session.id)
    assert binding.state is None and binding.status == LifecycleStatus.READY
    assert binding.data_version == 1 and binding.revision
    for specs in (
        (spec(), spec()),
        (spec("missing"),),
        (spec().model_copy(update={"config": {"label": []}}),),
    ):
        with pytest.raises((ValueError, LookupError)):
            await create(service, *specs)
    async with database.sessions.begin() as db:
        assert (await db.execute(select(func.count()).select_from(SessionRow))).scalar_one() == 1
        assert (
            await db.execute(select(func.count()).select_from(PluginBindingRow))
        ).scalar_one() == 1


async def test_state_copies_uuid_cas_scope_and_close(plugin_setup):
    service, plugins, _, _, contexts, _, _ = plugin_setup
    session = await create(service, spec())
    async with plugins.open_execution(session.id):
        ctx = contexts[-1]
        old = await ctx.state.read()
        updated = await ctx.state.replace(
            State(resources=["resource"]), expected_revision=old.revision
        )
        assert updated.revision != old.revision
        updated.value.resources.clear()
        assert (await ctx.state.read()).value.resources == ["resource"]
        with pytest.raises(StateConflict):
            await ctx.state.replace(State(), expected_revision=old.revision)
        ctx.config.label = "memory only"
        assert (await plugins.list_bindings(session.id))[0].config == {"label": "memory"}
        assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
        with pytest.raises(LifecycleError):
            await ctx.state.replace(State(), expected_revision=updated.revision)
        with pytest.raises(LifecycleError):
            await ctx.state.read()
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
    assert (
        await service.update_session(session.id, UpdateSession(title="archived"))
    ).title == "archived"
    with pytest.raises(LifecycleError):
        await service.enqueue_input(session.id, "queued", "late")
    assert await service.read_inputs(session.id, "queued") == ()


async def test_sequential_close_stops_and_retry_preserves_progress(plugin_setup):
    service, plugins, registry, definition, _, events, controls = plugin_setup
    registry.register(replace(definition, plugin_name="z_last"))
    registry.register(replace(definition, plugin_name="a_first"))
    session = await create(service, spec("z_last"), spec(), spec("a_first"))
    before = {b.plugin_name: b.revision for b in await plugins.list_bindings(session.id)}
    controls["fail_close"] = "memory"
    with pytest.raises(PluginOperationError, match="acme.memory"):
        await service.close_session(session.id)
    assert events == [("a_first", "close"), ("memory", "close")]
    assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING
    assert [b.status for b in await plugins.list_bindings(session.id)] == [
        LifecycleStatus.CLOSED,
        LifecycleStatus.CLOSING,
        LifecycleStatus.CLOSING,
    ]
    assert (await plugins.list_bindings(session.id))[1].revision == before["memory"]
    controls["fail_close"] = None
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
    assert events == [
        ("a_first", "close"),
        ("memory", "close"),
        ("memory", "close"),
        ("z_last", "close"),
    ]


async def test_concurrent_close_and_late_resource_registration(database):
    entered, finish = asyncio.Event(), asyncio.Event()
    contexts = []

    class Plugin(AgentPlugin[Config, State]):
        @asynccontextmanager
        async def open_execution(self, ctx):
            contexts.append(ctx)
            yield PluginBinding()

        async def close_session(self, ctx):
            entered.set()
            await finish.wait()
            # A competing closer may already have committed closed.
            await ctx.state.read()

    registry = PluginRegistry()
    registry.register(PluginDefinition("p", "r", 1, Config, State, Plugin))
    plugins = AgentPluginService(database.sessions, registry)
    service = SessionService(database.sessions, plugin_service=plugins)
    session = await create(service, spec("r", "p"))
    async with plugins.open_execution(session.id):
        old = await contexts[0].state.read()
        closing = asyncio.create_task(service.close_session(session.id))
        await entered.wait()
        with pytest.raises(LifecycleError):
            await contexts[0].state.replace(
                State(resources=["late"]), expected_revision=old.revision
            )
        assert (await plugins.list_bindings(session.id))[0].revision == old.revision
        competing = asyncio.create_task(service.close_session(session.id))
        finish.set()
        results = await asyncio.gather(closing, competing)
    assert all(item.status == LifecycleStatus.CLOSED for item in results)
    assert (await plugins.list_bindings(session.id))[0].state is None


async def test_loading_migrates_config_and_state_atomically(plugin_setup, database):
    service, plugins, _, definition, contexts, _, _ = plugin_setup
    session = await create(service, spec())
    before = (await plugins.list_bindings(session.id))[0]
    migrated = []

    def migrate(data):
        migrated.append(data)
        return PluginData(
            {"label": data.config["label"] + " upgraded"}, {"resources": ["old_resource"]}
        )

    registry = PluginRegistry()
    registry.register(replace(definition, data_version=2, migrations={1: migrate}))
    upgraded = AgentPluginService(database.sessions, registry)
    assert (await upgraded.list_bindings(session.id))[0].data_version == 1
    async with upgraded.open_execution(session.id):
        ctx = contexts[-1]
        assert ctx.config.label == "memory upgraded"
        assert (await ctx.state.read()).value.resources == ["old_resource"]
        persisted = (await upgraded.list_bindings(session.id))[0]
        assert persisted.data_version == 2 and persisted.revision != before.revision
    async with upgraded.open_execution(session.id):
        pass
    assert len(migrated) == 1
    with pytest.raises(PluginOperationError) as error:
        async with plugins.open_execution(session.id):
            pass
    assert "newer" in str(error.value.__cause__)
    assert (await upgraded.list_bindings(session.id))[0] == persisted


async def test_missing_or_invalid_migration_never_partially_persists(plugin_setup, database):
    service, plugins, _, definition, _, _, _ = plugin_setup
    session = await create(service, spec())
    before = (await plugins.list_bindings(session.id))[0]
    for migrations in ({}, {1: lambda data: PluginData({"label": []}, None)}):
        registry = PluginRegistry()
        registry.register(replace(definition, data_version=2, migrations=migrations))
        upgraded = AgentPluginService(database.sessions, registry)
        with pytest.raises(PluginOperationError):
            async with upgraded.open_execution(session.id):
                pass
        assert (await upgraded.list_bindings(session.id))[0] == before


async def test_native_tools_instructions_state_and_fresh_execution(
    plugin_setup, seed_session, session_model
):
    service, plugins, _, _, contexts, events, _ = plugin_setup
    session = await create(service, spec())
    # Keep the generated binding, while resolving the session model using the fixture's catalog.
    other_id = uuid4()
    await seed_session(other_id)
    model_session = await service.get_session(other_id)
    await service.update_session(
        session.id,
        UpdateSession(provider_id=model_session.provider_id, model_name=model_session.model_name),
    )
    requests = []

    def model(messages, info):
        requests.append(info)
        assert "Use memory memory." in (info.instructions or "")
        if isinstance(messages[-1].parts[0], UserPromptPart):
            return ModelResponse(parts=[ToolCallPart("acme_memory_remember", {"value": "saved"})])
        return ModelResponse(parts=[TextPart("done")])

    session_model(FunctionModel(model))
    for text in ("first", "second"):
        await service.enqueue_input(session.id, "queued", text)
        result = await service.start_runner(session.id)
        assert result.output == "done"
    assert events == [("memory", "enter"), ("memory", "exit")] * 2
    assert contexts[0] is not contexts[1]
    (binding,) = await plugins.list_bindings(session.id)
    assert binding.state == {"resources": ["saved", "saved"]}
    assert all(request.function_tools[0].name == "acme_memory_remember" for request in requests)


async def test_native_capabilities_isolate_runs_and_rewrite_before_checkpoint(
    plugin_setup, seed_session, session_model
):
    service, _, _, _, contexts, events, controls = plugin_setup
    template_id = uuid4()
    await seed_session(template_id)
    template = await service.get_session(template_id)
    session = await service.create_session(
        CreateSession(
            provider_id=template.provider_id,
            model_name=template.model_name,
            plugins=[spec()],
            compaction_threshold_tokens=1,
        )
    )
    runs, before_commits = [], []

    class Rewrite(AbstractCapability):
        def __init__(self, context):
            self.context = context
            self.requests = 0

        async def for_run(self, ctx):
            fresh = Rewrite(self.context)
            runs.append(fresh)
            return fresh

        def get_instructions(self):
            return "native instructions"

        async def before_model_request(self, ctx, request_context):
            await self.context.state.read()
            self.requests += 1
            return request_context

        async def after_model_request(self, ctx, *, request_context, response):
            response.parts[0].content += " rewritten"
            return response

        async def after_node_run(self, ctx, *, node, result):
            if isinstance(node, ModelRequestNode):
                if ctx.prompt != COMPACTION_PROMPT:
                    before_commits.append(len((await service.read_history(session.id)).items))
                result.model_response.parts.append(TextPart("after node"))
            return result

    def native_tool() -> str:
        return "native"

    def capabilities(ctx):
        return (
            PrefixTools(wrapped=Capability(tools=[native_tool]), prefix="already"),
            Rewrite(ctx),
        )

    controls["capabilities"] = capabilities

    def model(messages, info):
        assert {tool.name for tool in info.function_tools} == {
            "acme_memory_remember",
            "already_native_tool",
        }
        assert (info.instructions or "").count("native instructions") == 1
        assert (info.instructions or "").count("Use memory memory.") == 1
        summary = messages[-1].parts[-1].content == COMPACTION_PROMPT
        return ModelResponse(
            parts=[TextPart("summary" if summary else "business")],
            usage=RequestUsage(input_tokens=2),
        )

    session_model(FunctionModel(model))
    await service.enqueue_input(session.id, "steer", "first")
    await service.enqueue_input(session.id, "queued", "second")
    result = await service.start_runner(session.id)
    assert result.output == "business rewrittenafter node"
    assert len(runs) == 4 and all(run.requests == 1 for run in runs)
    assert before_commits == [1, 3]
    assert events == [("memory", "enter"), ("memory", "exit")]
    saved = (await service.read_history(session.id)).items
    assert len(saved) == 4
    for item in saved[1::2]:
        assert item.message.parts == [TextPart("business rewritten"), TextPart("after node")]
    with pytest.raises(LifecycleError):
        await contexts[0].state.read()


@pytest.mark.parametrize("name", ["acme_memory_remember", "invalid-name", "x" * 65])
async def test_native_tool_names_are_checked_after_composition(
    plugin_setup, seed_session, session_model, name
):
    service, _, _, _, _, events, controls = plugin_setup
    template_id = uuid4()
    await seed_session(template_id)
    template = await service.get_session(template_id)
    session = await service.create_session(
        CreateSession(
            provider_id=template.provider_id, model_name=template.model_name, plugins=[spec()]
        )
    )
    controls["capabilities"] = lambda ctx: (Capability(tools=[Tool(lambda: "native", name=name)]),)

    def model(messages, info):
        pytest.fail("Invalid tools must fail before the model request")

    session_model(FunctionModel(model))
    await service.enqueue_input(session.id, "queued", "go")
    error = UserError if name == "acme_memory_remember" else ValueError
    with pytest.raises(error):
        await service.start_runner(session.id)
    assert events == [("memory", "enter"), ("memory", "exit")]
    assert not await service.is_runner_running(session.id)


@pytest.mark.parametrize("wrapper", ["invalid_prefix", "long_prefix", "valid_rename"])
async def test_native_tool_names_are_checked_after_wrapper_toolsets(
    plugin_setup, seed_session, session_model, wrapper
):
    service, _, _, _, _, events, controls = plugin_setup
    template_id = uuid4()
    await seed_session(template_id)
    template = await service.get_session(template_id)
    session = await service.create_session(
        CreateSession(
            provider_id=template.provider_id, model_name=template.model_name, plugins=[spec()]
        )
    )
    calls = []

    def native_tool() -> str:
        calls.append("executed")
        return "native result"

    class WrappedCapability(Capability):
        def get_wrapper_toolset(self, toolset):
            if wrapper == "valid_rename":
                return RenamedToolset(toolset, name_map={"valid_name": "invalid-name"})
            prefix = "invalid-prefix" if wrapper == "invalid_prefix" else "x" * 64
            return PrefixedToolset(toolset, prefix=prefix)

    original_name = "invalid-name" if wrapper == "valid_rename" else "valid_tool"
    controls["capabilities"] = lambda ctx: (
        WrappedCapability(tools=[Tool(native_tool, name=original_name)]),
    )

    def model(messages, info):
        if wrapper != "valid_rename":
            pytest.fail("Names produced by wrappers must be checked before the model request")
        assert {tool.name for tool in info.function_tools} == {"acme_memory_remember", "valid_name"}
        if isinstance(messages[-1].parts[0], UserPromptPart):
            return ModelResponse(parts=[ToolCallPart("valid_name", {})])
        return ModelResponse(parts=[TextPart("done")])

    session_model(FunctionModel(model))
    await service.enqueue_input(session.id, "queued", "go")
    if wrapper == "valid_rename":
        assert (await service.start_runner(session.id)).output == "done"
        assert calls == ["executed"]
    else:
        with pytest.raises(ValueError):
            await service.start_runner(session.id)
        assert calls == []
    assert events == [("memory", "enter"), ("memory", "exit")]
    assert not await service.is_runner_running(session.id)


async def test_open_failure_keeps_input_and_releases_lease(plugin_setup, seed_session):
    service, _, registry, definition, contexts, events, controls = plugin_setup

    class FirstPlugin(AgentPlugin[Config, State]):
        @asynccontextmanager
        async def open_execution(self, ctx):
            contexts.append(ctx)
            events.append((ctx.plugin_name, "enter"))
            try:
                yield PluginBinding()
            finally:
                await ctx.state.read()
                events.append((ctx.plugin_name, "cleanup-state-read"))
                events.append((ctx.plugin_name, "exit"))

    registry.register(replace(definition, plugin_name="a_first", plugin_type=FirstPlugin))
    session = await create(service, spec(), spec("a_first"))
    other_id = uuid4()
    await seed_session(other_id)
    other = await service.get_session(other_id)
    await service.update_session(
        session.id, UpdateSession(provider_id=other.provider_id, model_name=other.model_name)
    )
    queued = await service.enqueue_input(session.id, "queued", "pending")
    controls["open_error"] = True
    with pytest.raises(PluginOperationError):
        await service.start_runner(session.id)
    assert await service.read_inputs(session.id, "queued") == (queued,)
    assert not await service.is_runner_running(session.id)
    assert events == [
        ("a_first", "enter"),
        ("memory", "enter"),
        ("memory", "exit"),
        ("a_first", "cleanup-state-read"),
        ("a_first", "exit"),
    ]
    assert (await service.get_session(session.id)).status == LifecycleStatus.READY
    with pytest.raises(LifecycleError):
        await contexts[0].state.read()


async def test_lower_runner_factory_runs_inside_lease_and_borrows_no_business_session(database):
    session_id = uuid4()
    events = []

    @asynccontextmanager
    async def factory():
        async with database.sessions.begin() as db:
            assert await is_session_busy(db, session_id)
        events.append("enter")
        try:
            yield RunnerExecution(Agent())
        finally:
            async with database.sessions.begin() as db:
                assert await is_session_busy(db, session_id)
            events.append("exit")

    async with open_runner(
        session_id, session_factory=database.sessions, execution_factory=factory
    ):
        assert events == ["enter"]
    assert events == ["enter", "exit"]
    async with database.sessions.begin() as db:
        assert not await is_session_busy(db, session_id)
    with pytest.raises(ValueError):
        async with open_runner(
            session_id, session_factory=database.sessions, agent=Agent(), execution_factory=factory
        ):
            pass


async def test_http_close_status_and_rejected_input(plugin_setup):
    service, _, _, _, _, _, _ = plugin_setup
    session = await create(service, spec())
    app = FastAPI()
    app.include_router(create_session_router(service), prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.post(f"/api/sessions/{session.id}/close")).json()["status"] == "closed"
        assert (
            await client.post(f"/api/sessions/{session.id}/inputs", json={"content": "late"})
        ).status_code == 409
        assert (
            await client.patch(f"/api/sessions/{session.id}", json={"title": "closed title"})
        ).status_code == 200
        assert (await client.get(f"/api/sessions/{session.id}/history")).status_code == 200
        assert (await client.post(f"/api/sessions/{session.id}/close")).status_code == 200


async def test_registry_rejects_duplicate_identity_and_invalid_versions(plugin_setup):
    _, _, registry, definition, _, _, _ = plugin_setup
    with pytest.raises(ValueError):
        registry.register(definition)
    for version in (True, 0, -1):
        with pytest.raises(ValueError):
            replace(definition, data_version=version)
    with pytest.raises(ValueError):
        replace(definition, data_version=2, migrations={True: lambda data: data})
    with pytest.raises(ValidationError):
        PluginSpec(plugin_provider="p.q", plugin_name="r", config={})


@pytest.mark.parametrize("kind", ["sync", "async", "awaitable"])
async def test_adapter_preserves_native_dispatch_validation_and_scope(plugin_setup, kind):
    from pydantic_ai.messages import RetryPromptPart, ToolReturnPart

    from kapy.agent_plugins.capability import PluginCapabilityAdapter

    service, plugins, _, _, _, _, _ = plugin_setup
    session = await create(service, spec())
    called = []

    def sync(ctx: int) -> int:
        called.append(ctx)
        return ctx + 1

    async def asynchronous(ctx: int) -> int:
        return sync(ctx)

    def awaitable(ctx: int):
        return asynchronous(ctx)

    function = {"sync": sync, "async": asynchronous, "awaitable": awaitable}[kind]

    def model(messages, info):
        part = messages[-1].parts[0]
        if isinstance(part, UserPromptPart):
            return ModelResponse(
                parts=[ToolCallPart("acme_memory_increment", {"ctx": 2 if called else "bad"})]
            )
        if isinstance(part, RetryPromptPart):
            assert called == []
            return ModelResponse(parts=[ToolCallPart("acme_memory_increment", {"ctx": 2})])
        assert isinstance(part, ToolReturnPart)
        assert part.content == 3
        return ModelResponse(parts=[TextPart("3")])

    async with plugins.open_execution(session.id) as bindings:
        capabilities = PluginCapabilityAdapter.build(
            "acme",
            "memory",
            PluginBinding(tools=(PluginTool("increment", "Increment an integer", function),)),
            bindings[0][3],
            set(),
        )
        agent = Agent(FunctionModel(model), capabilities=capabilities)
        assert (await agent.run("increment")).output == "3"
        assert called == [2]
        await service.close_session(session.id)
        with pytest.raises(LifecycleError):
            await agent.run("increment")
        assert called == [2]


async def test_config_and_state_encoding_round_trips_aliases_and_json(plugin_setup, database):
    from pydantic import Json, JsonValue

    class Data(BaseModel):
        label: str = Field(alias="resource_label")
        numbers: Json[list[int]]

    service, plugins, _, definition, contexts, _, _ = plugin_setup
    registry = PluginRegistry()
    registry.register(replace(definition, config_type=Data, state_type=Data))
    encoded_plugins = AgentPluginService(database.sessions, registry)
    encoded_service = SessionService(database.sessions, plugin_service=encoded_plugins)
    raw: dict[str, JsonValue] = {"resource_label": "ready", "numbers": "[1,2]"}
    session = await create(encoded_service, spec().model_copy(update={"config": raw}))
    async with encoded_plugins.open_execution(session.id):
        context = contexts[-1]
        assert context.config.label == "ready" and context.config.numbers == [1, 2]
        initial = await context.state.read()
        await context.state.replace(Data.model_validate(raw), expected_revision=initial.revision)
        assert (await context.state.read()).value.numbers == [1, 2]
    async with encoded_plugins.open_execution(session.id):
        assert contexts[-1].config.numbers == [1, 2]
        assert (await contexts[-1].state.read()).value.label == "ready"
    (binding,) = await encoded_plugins.list_bindings(session.id)
    assert binding.config == binding.state == raw

    old = await create(service, spec())
    registry = PluginRegistry()
    registry.register(
        replace(
            definition,
            config_type=Data,
            state_type=Data,
            data_version=2,
            migrations={1: lambda data: PluginData(raw, raw)},
        )
    )
    upgraded = AgentPluginService(database.sessions, registry)
    async with upgraded.open_execution(old.id):
        assert contexts[-1].config.numbers == [1, 2]
        assert (await contexts[-1].state.read()).value.numbers == [1, 2]
    (after,) = await upgraded.list_bindings(old.id)
    assert after.data_version == 2 and after.config == after.state == raw
    async with upgraded.open_execution(old.id):
        assert contexts[-1].config.label == "ready"


async def test_unreadable_serializer_output_is_never_persisted(plugin_setup, database):
    from pydantic import field_serializer

    class Unreadable(BaseModel):
        label: str

        @field_serializer("label")
        def serialize_label(self, value):
            return []

    service, plugins, _, definition, contexts, _, _ = plugin_setup
    registry = PluginRegistry()
    registry.register(replace(definition, config_type=Unreadable, state_type=Unreadable))
    invalid_plugins = AgentPluginService(database.sessions, registry)
    invalid_service = SessionService(database.sessions, plugin_service=invalid_plugins)
    with pytest.raises(ValidationError):
        await create(invalid_service, spec())
    assert (await service.list_sessions()).items == []

    session = await create(service, spec())
    (before,) = await plugins.list_bindings(session.id)
    registry = PluginRegistry()
    registry.register(replace(definition, state_type=Unreadable))
    state_plugins = AgentPluginService(database.sessions, registry)
    async with state_plugins.open_execution(session.id):
        with pytest.raises(ValidationError):
            await contexts[-1].state.replace(
                Unreadable(label="valid input"), expected_revision=before.revision
            )
    assert (await plugins.list_bindings(session.id))[0] == before

    registry = PluginRegistry()
    registry.register(
        replace(
            definition, config_type=Unreadable, data_version=2, migrations={1: lambda data: data}
        )
    )
    upgrading = AgentPluginService(database.sessions, registry)
    with pytest.raises(PluginOperationError):
        async with upgrading.open_execution(session.id):
            pass
    assert (await plugins.list_bindings(session.id))[0] == before


async def test_json_null_root_config_can_be_created_loaded_and_closed(database):
    from pydantic import RootModel
    from sqlalchemy import text

    seen = []

    class Plugin(AgentPlugin[RootModel[None], State]):
        @asynccontextmanager
        async def open_execution(self, ctx):
            seen.append(ctx.config.root)
            yield PluginBinding()

    registry = PluginRegistry()
    registry.register(PluginDefinition("p", "null", 1, RootModel[None], State, Plugin))
    plugins = AgentPluginService(database.sessions, registry)
    service = SessionService(database.sessions, plugin_service=plugins)
    session = await create(
        service, PluginSpec(plugin_provider="p", plugin_name="null", config=None)
    )
    async with database.sessions.begin() as db:
        is_json_null = (
            await db.execute(
                text(
                    "SELECT config::text = 'null' AND config IS NOT NULL "
                    "FROM plugin_agent_bindings WHERE session_id = :id"
                ),
                {"id": session.id},
            )
        ).scalar_one()
        assert is_json_null
    async with plugins.open_execution(session.id):
        pass
    assert seen == [None]
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED


@pytest.mark.parametrize("suppress_context", [False, True])
async def test_concurrent_close_preserves_cleanup_error_before_lifecycle_error(
    database, suppress_context
):
    first_entered, second_closed = asyncio.Event(), asyncio.Event()
    count = 0

    class Plugin(AgentPlugin[Config, State]):
        @asynccontextmanager
        async def open_execution(self, ctx):
            yield PluginBinding()

        async def close_session(self, ctx):
            nonlocal count
            count += 1
            if count == 1:
                first_entered.set()
                await second_closed.wait()
                try:
                    raise RuntimeError("real external cleanup failure")
                finally:
                    try:
                        await ctx.state.read()
                    except LifecycleError as error:
                        if suppress_context:
                            raise error from None
                        raise

    registry = PluginRegistry()
    registry.register(PluginDefinition("p", "close", 1, Config, State, Plugin))
    plugins = AgentPluginService(database.sessions, registry)
    service = SessionService(database.sessions, plugin_service=plugins)
    session = await create(service, spec("close", "p"))
    first = asyncio.create_task(service.close_session(session.id))
    try:
        await first_entered.wait()
        assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
        second_closed.set()
        with pytest.raises(PluginOperationError, match="p.close") as caught:
            await first
        lifecycle_error = caught.value.__cause__
        assert isinstance(lifecycle_error, LifecycleError)
        assert isinstance(lifecycle_error.__context__, RuntimeError)
        assert str(lifecycle_error.__context__) == "real external cleanup failure"
    finally:
        second_closed.set()
        await asyncio.gather(first, return_exceptions=True)


async def test_close_stops_late_ordinary_tool_and_preserves_next_input(
    database, seed_session, session_model
):
    from kapy.control.sessions.repository import SessionRepository

    service = SessionService(database.sessions)
    session_id = uuid4()
    await seed_session(session_id)
    model_entered, release_model = asyncio.Event(), asyncio.Event()
    tool_calls = []

    async def ordinary_tool() -> str:
        tool_calls.append("called")
        return "result"

    async def model(messages, info):
        model_entered.set()
        await release_model.wait()
        return ModelResponse(parts=[ToolCallPart("ordinary_tool", {})])

    session_model(FunctionModel(model))
    agent = Agent(tools=[ordinary_tool])
    await service.enqueue_input(session_id, "queued", "first")
    running = asyncio.create_task(service.start_runner(session_id, agent=agent))
    try:
        await asyncio.wait_for(model_entered.wait(), 5)
        queued = await service.enqueue_input(session_id, "queued", "next")
        closed = await asyncio.wait_for(service.close_session(session_id), 2)
        assert closed.status == LifecycleStatus.CLOSED
        assert not running.done() and not release_model.is_set()
        # Remove the one-shot advisory signal so it cannot mask persistent checks.
        async with database.sessions.begin() as db:
            assert await SessionRepository(db).consume_cancel(session_id)
        assert not await service.read_cancel(session_id)
        release_model.set()
        with pytest.raises(LifecycleError):
            await asyncio.wait_for(running, 5)
        assert tool_calls == []
        assert await service.read_inputs(session_id, "queued") == (queued,)
        assert not await service.is_runner_running(session_id)
        with pytest.raises(LifecycleError):
            await service.start_runner(session_id, agent=agent)
        assert await service.read_inputs(session_id, "queued") == (queued,)
        assert tool_calls == []
    finally:
        release_model.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_closing_commit_serializes_resource_registration_and_enqueue(
    database, monkeypatch, wait_for_lock
):
    from sqlalchemy import text

    from kapy.agent_plugins import repository as binding_repository
    from kapy.control.sessions import service as session_service
    from kapy.control.sessions.repository import SessionRepository

    contexts = []

    class Plugin(AgentPlugin[Config, State]):
        @asynccontextmanager
        async def open_execution(self, ctx):
            contexts.append(ctx)
            yield PluginBinding()

    registry = PluginRegistry()
    registry.register(PluginDefinition("p", "resource", 1, Config, State, Plugin))
    plugins = AgentPluginService(database.sessions, registry)
    service = SessionService(database.sessions, plugin_service=plugins)
    session = await create(service, spec("resource", "p"))
    closing_written, commit_closing = asyncio.Event(), asyncio.Event()
    backend_pids = asyncio.Queue()
    set_cancel = SessionRepository.set_cancel
    lock_session = binding_repository.lock_session

    async def hold_closing(repo, session_id):
        await set_cancel(repo, session_id)
        closing_written.set()
        await commit_closing.wait()

    async def observe_lock(db, session_id):
        task = asyncio.current_task()
        if task is not None and task.get_name() in {"register-resource", "enqueue-input"}:
            pid = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            backend_pids.put_nowait(pid)
        return await lock_session(db, session_id)

    monkeypatch.setattr(SessionRepository, "set_cancel", hold_closing)
    monkeypatch.setattr(binding_repository, "lock_session", observe_lock)
    monkeypatch.setattr(session_service, "lock_session", observe_lock)
    tasks: list[asyncio.Task] = []
    async with plugins.open_execution(session.id):
        state = contexts[-1].state
        before = await state.read()
        closing = asyncio.create_task(service.close_session(session.id))
        tasks.append(closing)
        try:
            await asyncio.wait_for(closing_written.wait(), 5)
            registration = asyncio.create_task(
                state.replace(State(resources=["late"]), expected_revision=before.revision),
                name="register-resource",
            )
            enqueue = asyncio.create_task(
                service.enqueue_input(session.id, "queued", "late"), name="enqueue-input"
            )
            tasks.extend([registration, enqueue])
            for _ in range(2):
                pid = await asyncio.wait_for(backend_pids.get(), 5)
                await wait_for_lock(pid)
            assert not registration.done() and not enqueue.done()
            commit_closing.set()
            assert (await asyncio.wait_for(closing, 5)).status == LifecycleStatus.CLOSED
            for operation in (registration, enqueue):
                with pytest.raises(LifecycleError):
                    await asyncio.wait_for(operation, 5)
            (binding,) = await plugins.list_bindings(session.id)
            assert binding.state is None and binding.revision == before.revision
            assert await service.read_inputs(session.id, "queued") == ()
        finally:
            commit_closing.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_sdk_instructions_reject_state_writes_and_restore_tool_permissions(plugin_setup):
    from pydantic_ai.messages import ToolReturnPart

    from kapy.agent_plugins.capability import PluginCapabilityAdapter

    service, plugins, _, _, contexts, _, _ = plugin_setup
    session = await create(service, spec())
    evaluated = []

    async with plugins.open_execution(session.id) as bindings:
        context = contexts[-1]
        initial = await context.state.read()

        async def instructions():
            before = await context.state.read()
            with pytest.raises(LifecycleError, match="instructions"):
                await context.state.replace(
                    State(resources=["forbidden"]), expected_revision=before.revision
                )
            assert await context.state.read() == before
            evaluated.append(True)
            return "Remember the requested value."

        provider, name, binding, store = bindings[0]
        capabilities = PluginCapabilityAdapter.build(
            provider, name, replace(binding, instructions=instructions), store, set()
        )

        def model(messages, info):
            assert "Remember the requested value." in (info.instructions or "")
            if isinstance(messages[-1].parts[0], UserPromptPart):
                return ModelResponse(
                    parts=[ToolCallPart("acme_memory_remember", {"value": "saved"})]
                )
            assert isinstance(messages[-1].parts[0], ToolReturnPart)
            return ModelResponse(parts=[TextPart("done")])

        agent = Agent(FunctionModel(model), capabilities=capabilities)
        assert (await agent.run("remember")).output == "done"
        after = await context.state.read()
        assert evaluated and after.value.resources == ["saved"]
        assert after.revision != initial.revision
    (binding_record,) = await plugins.list_bindings(session.id)
    assert binding_record.state == {"resources": ["saved"]}

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
from kapy.session_lease import SessionBusy, is_session_busy, open_session_lease

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


@asynccontextmanager
async def open_plugins(plugins, session_id):
    async with open_session_lease(session_id, session_factory=plugins.session_factory) as lease:
        async with plugins.open_execution(session_id, lease=lease) as bindings:
            yield bindings


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
    async with open_plugins(plugins, session.id):
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
        with pytest.raises(SessionBusy):
            await service.close_session(session.id)
        assert (await ctx.state.read()).value.resources == ["resource"]
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
        LifecycleStatus.READY,
        LifecycleStatus.READY,
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


async def test_busy_close_preserves_execution_cancel_and_inputs(plugin_setup):
    service, plugins, _, _, contexts, events, _ = plugin_setup
    session = await create(service, spec())
    pending = await service.enqueue_input(session.id, "queued", "keep pending")
    await service.request_cancel(session.id)
    async with open_plugins(plugins, session.id):
        before = await plugins.list_bindings(session.id)
        with pytest.raises(SessionBusy):
            await service.close_session(session.id)
        assert await plugins.list_bindings(session.id) == before
        assert (await service.get_session(session.id)).status == LifecycleStatus.READY
        assert await service.read_cancel(session.id)
        assert await service.read_inputs(session.id, "queued") == (pending,)
        previous = await contexts[-1].state.read()
        await contexts[-1].state.replace(
            State(resources=["still usable"]), expected_revision=previous.revision
        )
    assert events == [("memory", "enter"), ("memory", "exit")]
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
    assert await service.read_cancel(session.id)
    assert await service.read_inputs(session.id, "queued") == (pending,)


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
    async with open_plugins(upgraded, session.id):
        ctx = contexts[-1]
        assert ctx.config.label == "memory upgraded"
        assert (await ctx.state.read()).value.resources == ["old_resource"]
        persisted = (await upgraded.list_bindings(session.id))[0]
        assert persisted.data_version == 2 and persisted.revision != before.revision
    async with open_plugins(upgraded, session.id):
        pass
    assert len(migrated) == 1
    with pytest.raises(PluginOperationError) as error:
        async with open_plugins(plugins, session.id):
            pass
    assert "newer" in str(error.value.__cause__)
    assert (await upgraded.list_bindings(session.id))[0] == persisted


async def test_migration_fences_owner_replaced_after_binding_load(
    database, plugin_setup, monkeypatch
):
    import psycopg

    from kapy.session_lease import LeaseLost

    service, plugins, _, definition, contexts, events, _ = plugin_setup
    session = await create(service, spec())
    before = (await plugins.list_bindings(session.id))[0]
    migrations = []

    def migrate(data):
        migrations.append(data)
        return PluginData({"label": "upgraded"}, {"resources": ["preserved resource"]})

    registry = PluginRegistry()
    upgraded_definition = replace(definition, data_version=2, migrations={1: migrate})
    registry.register(upgraded_definition)
    upgraded = AgentPluginService(database.sessions, registry)
    load = PluginDefinition.load

    def load_then_replace_owner(self, version, data):
        loaded = load(self, version, data)
        # The initial binding-read transaction has ended. Inject another owner's
        # committed token here, independently of the migration save/fencing path.
        # This test hook uses a separate real connection; migrate itself stays pure.
        with psycopg.connect(
            database.url, options=f"-csearch_path={database.schema},pg_catalog"
        ) as db:
            db.execute(
                "UPDATE session_leases SET lock_token=%s WHERE session_id=%s",
                (uuid4(), session.id),
            )
        return loaded

    monkeypatch.setattr(PluginDefinition, "load", load_then_replace_owner)
    with pytest.raises(PluginOperationError) as caught:
        async with open_plugins(upgraded, session.id):
            pytest.fail("lost owner entered plugin execution")
    assert isinstance(caught.value.__cause__, LeaseLost)
    assert len(migrations) == 1
    assert not contexts and not events
    assert (await plugins.list_bindings(session.id))[0] == before


async def test_missing_or_invalid_migration_never_partially_persists(plugin_setup, database):
    service, plugins, _, definition, _, _, _ = plugin_setup
    session = await create(service, spec())
    before = (await plugins.list_bindings(session.id))[0]
    for migrations in ({}, {1: lambda data: PluginData({"label": []}, None)}):
        registry = PluginRegistry()
        registry.register(replace(definition, data_version=2, migrations=migrations))
        upgraded = AgentPluginService(database.sessions, registry)
        with pytest.raises(PluginOperationError):
            async with open_plugins(upgraded, session.id):
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
    async def factory(lease):
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

    async with open_plugins(plugins, session.id) as bindings:
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
    async with open_plugins(encoded_plugins, session.id):
        context = contexts[-1]
        assert context.config.label == "ready" and context.config.numbers == [1, 2]
        initial = await context.state.read()
        await context.state.replace(Data.model_validate(raw), expected_revision=initial.revision)
        assert (await context.state.read()).value.numbers == [1, 2]
    async with open_plugins(encoded_plugins, session.id):
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
    async with open_plugins(upgraded, old.id):
        assert contexts[-1].config.numbers == [1, 2]
        assert (await contexts[-1].state.read()).value.numbers == [1, 2]
    (after,) = await upgraded.list_bindings(old.id)
    assert after.data_version == 2 and after.config == after.state == raw
    async with open_plugins(upgraded, old.id):
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
    async with open_plugins(state_plugins, session.id):
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
        async with open_plugins(upgrading, session.id):
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
    async with open_plugins(plugins, session.id):
        pass
    assert seen == [None]
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED


async def test_competing_close_is_busy_and_failure_keeps_progress(database):
    entered, finish = asyncio.Event(), asyncio.Event()
    calls = []

    class Plugin(AgentPlugin[Config, State]):
        @asynccontextmanager
        async def open_execution(self, ctx):
            yield PluginBinding()

        async def close_session(self, ctx):
            calls.append(ctx.session_id)
            entered.set()
            await finish.wait()
            raise RuntimeError("external cleanup failure")

    registry = PluginRegistry()
    registry.register(PluginDefinition("p", "close", 1, Config, State, Plugin))
    plugins = AgentPluginService(database.sessions, registry)
    service = SessionService(database.sessions, plugin_service=plugins)
    session = await create(service, spec("close", "p"))
    first = asyncio.create_task(service.close_session(session.id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        with pytest.raises(SessionBusy):
            await service.close_session(session.id)
        assert calls == [session.id]
        assert not await service.read_cancel(session.id)
        finish.set()
        with pytest.raises(PluginOperationError) as caught:
            await first
        assert str(caught.value.__cause__) == "external cleanup failure"
        assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING
        assert (await plugins.list_bindings(session.id))[0].status == LifecycleStatus.READY
    finally:
        finish.set()
        await asyncio.gather(first, return_exceptions=True)


async def test_busy_close_does_not_cancel_model_or_ordinary_tools(
    database, seed_session, session_model
):
    service = SessionService(database.sessions)
    session_id = uuid4()
    await seed_session(session_id)
    model_entered, release_model = asyncio.Event(), asyncio.Event()
    tool_calls = []

    async def ordinary_tool() -> str:
        tool_calls.append("called")
        return "result"

    async def model(messages, info):
        if isinstance(messages[-1].parts[0], UserPromptPart):
            model_entered.set()
            await release_model.wait()
            return ModelResponse(parts=[ToolCallPart("ordinary_tool", {})])
        return ModelResponse(parts=[TextPart("done")])

    session_model(FunctionModel(model))
    agent = Agent(tools=[ordinary_tool])
    await service.enqueue_input(session_id, "queued", "first")
    running = asyncio.create_task(service.start_runner(session_id, agent=agent))
    try:
        await asyncio.wait_for(model_entered.wait(), 5)
        with pytest.raises(SessionBusy):
            await service.close_session(session_id)
        assert (await service.get_session(session_id)).status == LifecycleStatus.READY
        assert not await service.read_cancel(session_id)
        release_model.set()
        assert (await asyncio.wait_for(running, 5)).output == "done"
        assert tool_calls == ["called"]
        assert (await service.close_session(session_id)).status == LifecycleStatus.CLOSED
        with pytest.raises(LifecycleError):
            await service.start_runner(session_id, agent=agent)
    finally:
        release_model.set()
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


async def test_closing_decision_serializes_input_intake(database, monkeypatch, wait_for_lock):
    from sqlalchemy import text

    from kapy.agent_plugins.repository import BindingRepository
    from kapy.control.sessions import service as session_service

    service = SessionService(database.sessions)
    session = await create(service)
    closing_written, commit_closing = asyncio.Event(), asyncio.Event()
    backend_pid = asyncio.Future()
    list_bindings = BindingRepository.list
    lock_session = session_service.lock_session

    async def hold_closing(repo, session_id):
        result = await list_bindings(repo, session_id)
        closing_written.set()
        await commit_closing.wait()
        return result

    async def observe_lock(db, session_id):
        task = asyncio.current_task()
        if task is not None and task.get_name() == "enqueue-input":
            backend_pid.set_result((await db.execute(text("SELECT pg_backend_pid()"))).scalar_one())
        return await lock_session(db, session_id)

    monkeypatch.setattr(BindingRepository, "list", hold_closing)
    monkeypatch.setattr(session_service, "lock_session", observe_lock)
    closing = asyncio.create_task(service.close_session(session.id))
    tasks: list[asyncio.Task] = [closing]
    try:
        await asyncio.wait_for(closing_written.wait(), 5)
        enqueue = asyncio.create_task(
            service.enqueue_input(session.id, "queued", "late"), name="enqueue-input"
        )
        tasks.append(enqueue)
        await wait_for_lock(await asyncio.wait_for(backend_pid, 5))
        assert not enqueue.done()
        commit_closing.set()
        assert (await asyncio.wait_for(closing, 5)).status == LifecycleStatus.CLOSED
        with pytest.raises(LifecycleError):
            await enqueue
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

    async with open_plugins(plugins, session.id) as bindings:
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


async def test_parallel_state_replacements_keep_revision_cas(plugin_setup):
    service, plugins, _, _, contexts, _, _ = plugin_setup
    session = await create(service, spec())
    async with open_plugins(plugins, session.id):
        store = contexts[-1].state
        before = await store.read()
        results = await asyncio.gather(
            store.replace(State(resources=["a"]), expected_revision=before.revision),
            store.replace(State(resources=["b"]), expected_revision=before.revision),
            return_exceptions=True,
        )
        assert sum(isinstance(result, StateConflict) for result in results) == 1
        assert (await store.read()).value.resources in (["a"], ["b"])


@pytest.mark.parametrize("operation", ["read", "replace"])
async def test_plugin_state_fences_replaced_owner(database, plugin_setup, operation):
    from sqlalchemy import text

    from kapy.session_lease import LeaseLost

    service, plugins, _, _, contexts, _, _ = plugin_setup
    session = await create(service, spec())
    replacement = uuid4()
    with pytest.raises(LeaseLost):
        async with open_plugins(plugins, session.id):
            store = contexts[-1].state
            before = await store.read()
            async with database.sessions.begin() as db:
                await db.execute(
                    text("UPDATE session_leases SET lock_token=:token WHERE session_id=:id"),
                    {"id": session.id, "token": replacement},
                )
            with pytest.raises(LeaseLost):
                if operation == "read":
                    await store.read()
                else:
                    await store.replace(
                        State(resources=["stale"]), expected_revision=before.revision
                    )
    (binding,) = await plugins.list_bindings(session.id)
    assert binding.state is None and binding.revision == before.revision
    async with database.sessions.begin() as db:
        assert (
            await db.execute(
                text("SELECT lock_token FROM session_leases WHERE session_id=:id"),
                {"id": session.id},
            )
        ).scalar_one() == replacement


async def test_plugin_host_rejects_another_sessions_lease(database, plugin_setup):
    service, plugins, _, _, _, _, _ = plugin_setup
    session = await create(service, spec())
    async with open_session_lease(uuid4(), session_factory=database.sessions) as lease:
        with pytest.raises(ValueError, match="target session"):
            async with plugins.open_execution(session.id, lease=lease):
                pytest.fail("wrong lease accepted")
        with pytest.raises(ValueError, match="target session"):
            await plugins.close_binding((await plugins.list_bindings(session.id))[0], lease=lease)


async def test_close_lost_ownership_cannot_record_binding_completion(database):
    from sqlalchemy import text

    from kapy.session_lease import LeaseLost

    class Plugin(AgentPlugin[Config, State]):
        @asynccontextmanager
        async def open_execution(self, ctx):
            yield PluginBinding()

        async def close_session(self, ctx):
            # Simulate takeover while an already-issued external deletion completes.
            async with database.sessions.begin() as db:
                await db.execute(
                    text("UPDATE session_leases SET lock_token=:token WHERE session_id=:id"),
                    {"id": ctx.session_id, "token": uuid4()},
                )

    registry = PluginRegistry()
    registry.register(PluginDefinition("p", "close", 1, Config, State, Plugin))
    plugins = AgentPluginService(database.sessions, registry)
    service = SessionService(database.sessions, plugin_service=plugins)
    session = await create(service, spec("close", "p"))
    with pytest.raises(PluginOperationError) as caught:
        await service.close_session(session.id)
    assert isinstance(caught.value.__cause__, LeaseLost)
    assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING
    assert (await plugins.list_bindings(session.id))[0].status == LifecycleStatus.READY


async def test_http_busy_close_returns_conflict_without_mutation(database, plugin_setup):
    service, plugins, _, _, _, events, _ = plugin_setup
    session = await create(service, spec())
    before = await plugins.list_bindings(session.id)
    app = FastAPI()
    app.include_router(create_session_router(service), prefix="/api")
    async with (
        open_session_lease(session.id, session_factory=database.sessions),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        response = await client.post(f"/api/sessions/{session.id}/close")
    assert response.status_code == 409
    assert (await service.get_session(session.id)).status == LifecycleStatus.READY
    assert await plugins.list_bindings(session.id) == before
    assert not await service.read_cancel(session.id) and not events


async def test_close_waits_through_takeover_grace_without_deciding_closing(
    database, heartbeat_observation
):
    from sqlalchemy import text

    service = SessionService(database.sessions, heartbeat_interval=0.01, takeover_grace_period=0.3)
    session = await create(service)
    async with database.sessions.begin() as db:
        await db.execute(
            text(
                "INSERT INTO session_leases (session_id, lock_token, heartbeat_at) "
                "VALUES (:id, :token, clock_timestamp() - interval '1 hour')"
            ),
            {"id": session.id, "token": uuid4()},
        )
    _, renewed = heartbeat_observation
    closing = asyncio.create_task(service.close_session(session.id))
    try:
        await asyncio.wait_for(renewed.wait(), 5)
        assert (await service.get_session(session.id)).status == LifecycleStatus.READY
        pending = await service.enqueue_input(session.id, "queued", "accepted during grace")
        assert not await service.read_cancel(session.id)
        assert (await asyncio.wait_for(closing, 5)).status == LifecycleStatus.CLOSED
        assert await service.read_inputs(session.id, "queued") == (pending,)
    finally:
        closing.cancel()
        await asyncio.gather(closing, return_exceptions=True)


@pytest.mark.parametrize("cancellation", ["asyncio", "anyio"])
async def test_cancelled_close_releases_lease_and_retries_unfinished_bindings(
    database, cancellation
):
    import anyio

    entered, cleaned = asyncio.Event(), asyncio.Event()
    calls = []
    pause = True

    class Plugin(AgentPlugin[Config, State]):
        @asynccontextmanager
        async def open_execution(self, ctx):
            yield PluginBinding()

        async def close_session(self, ctx):
            calls.append(ctx.plugin_name)
            if ctx.plugin_name == "z_last" and pause:
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    with anyio.fail_after(1, shield=True):
                        await ctx.state.read()
                        cleaned.set()

    registry = PluginRegistry()
    for name in ("a_first", "z_last"):
        registry.register(PluginDefinition("p", name, 1, Config, State, Plugin))
    plugins = AgentPluginService(database.sessions, registry)
    service = SessionService(database.sessions, plugin_service=plugins)
    session = await create(service, spec("z_last", "p"), spec("a_first", "p"))
    if cancellation == "asyncio":
        task = asyncio.create_task(service.close_session(session.id))
        await asyncio.wait_for(entered.wait(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        async with anyio.create_task_group() as group:
            group.start_soon(service.close_session, session.id)
            await asyncio.wait_for(entered.wait(), 5)
            group.cancel_scope.cancel()
    assert cleaned.is_set()
    assert not await service.is_runner_running(session.id)
    assert (await service.get_session(session.id)).status == LifecycleStatus.CLOSING
    assert [b.status for b in await plugins.list_bindings(session.id)] == [
        LifecycleStatus.CLOSED,
        LifecycleStatus.READY,
    ]
    pause = False
    assert (await service.close_session(session.id)).status == LifecycleStatus.CLOSED
    assert calls == ["a_first", "z_last", "z_last"]

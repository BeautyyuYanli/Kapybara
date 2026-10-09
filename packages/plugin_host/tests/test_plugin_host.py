"""Public RPC contracts exercised through real SDK Agents and native composition."""

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Annotated, Any

import pytest
from plugin_host import (
    PluginHostCapability,
    PluginHostError,
    PluginManifest,
    build_plugin_host,
    describe_plugins,
    load_plugin_host,
)
from pydantic import BaseModel, Field, TypeAdapter
from pydantic_ai import Agent, CustomEvent, RunContext, Tool
from pydantic_ai.capabilities import CapabilityOrdering, Hooks
from pydantic_ai.exceptions import UnexpectedModelBehavior, UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage

type Handler = Callable[[dict[str, Any]], Any | Awaitable[Any]]

SCHEMA = {
    "type": "object",
    "properties": {"n": {"$ref": "#/$defs/positive"}},
    "$defs": {"positive": {"type": "integer", "minimum": 1}},
    "required": ["n"],
    "additionalProperties": False,
}
NAME = "add"


class Client:
    """In-memory implementation of the published RPC method/payload contract."""

    def __init__(self, manifest: dict[str, Any], handlers: dict[str, Handler] | None = None):
        self.manifest = manifest
        self.handlers = handlers or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        self.calls.append((method, deepcopy(params)))
        if method == "plugin.describe":
            return self.manifest
        if method == "plugin.invoke":
            value = self.handlers[params["handler"]](params["payload"])
            return await value if inspect.isawaitable(value) else value
        raise AssertionError(f"unexpected RPC method {method}")


def manifest(**values: Any) -> dict[str, Any]:
    return {"protocol_version": 1, "plugin_id": "demo", **values}


def add(n: Annotated[int, Field(ge=1)]) -> int:
    """Add one using an Agent-owned SDK tool, never a plugin export."""
    return n + 1


def hook(point: str, *, id: str | None = None, **kwargs: Any) -> dict[str, Any]:
    return {"id": id or point, "point": point, "handler": id or point, **kwargs}


def value(data: Any) -> dict[str, Any]:
    return {"kind": "value", "value": data}


def context() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage(), prompt="test")


def request() -> ModelRequestContext:
    return ModelRequestContext(
        model=TestModel(),
        messages=[ModelRequest.user_text_prompt("test")],
        model_settings={"temperature": 0.3},
        model_request_parameters=ModelRequestParameters(),
        model_id="local-test",
        streaming=True,
    )


def tool_model(args: dict[str, Any]) -> FunctionModel:
    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        for message in reversed(messages):
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, ToolReturnPart):
                        return ModelResponse(parts=[TextPart(json.dumps(part.content))])
        return ModelResponse(parts=[ToolCallPart(NAME, deepcopy(args))])

    return FunctionModel(respond)


async def test_agent_instructions_are_lazy_and_reusable():
    step = 0

    def instructions(payload: dict[str, Any]):
        nonlocal step
        step += 1
        assert payload == {}
        return value(None if step == 3 else f"step {step}")

    client = Client(
        manifest(
            instructions=[
                {"kind": "text", "text": "Static instruction"},
                {"kind": "rpc", "handler": "instructions"},
            ],
        ),
        {"instructions": instructions},
    )
    host = await load_plugin_host([client])
    seen: list[AgentInfo] = []
    model = tool_model({"n": 2})

    def respond(messages: list[ModelMessage], info: AgentInfo):
        seen.append(info)
        assert model.function is not None
        return model.function(messages, info)

    agent = Agent(
        FunctionModel(respond),
        capabilities=[host],
        tools=[add],
        deps_type=dict[str, str],
    )
    assert client.calls == [("plugin.describe", {})]
    async with agent:
        result = await agent.run("calculate", deps={"local": "private"})
        again = await agent.run("again", deps={"local": "other"})
    assert result.output == again.output == "3"
    assert step == 4
    assert [info.instructions for info in seen] == [
        "Static instruction\n\nstep 1",
        "Static instruction\n\nstep 2",
        "Static instruction",
        "Static instruction\n\nstep 4",
    ]


async def test_run_host_selection_is_isolated_and_keeps_native_ordering():
    traces: dict[str, list[str]] = {"pair": [], "other": [], "empty": []}
    ready = asyncio.Event()
    arrivals = 0

    def record(label: str) -> Handler:
        def callback(payload: dict[str, Any]):
            prompt = payload["request"]["messages"][0]["parts"][0]["content"]
            traces[prompt].append(label)
            return {"kind": "keep"}

        return callback

    def client(plugin_id: str, *, wraps: str | None = None) -> Client:
        async def instruction(payload: dict[str, Any]):
            nonlocal arrivals
            assert payload == {}
            if plugin_id in ("right", "other"):
                arrivals += 1
                if arrivals == 2:
                    ready.set()
                await ready.wait()
            return value(f"{plugin_id} dynamic")

        return Client(
            manifest(
                plugin_id=plugin_id,
                instructions=[
                    {"kind": "text", "text": f"{plugin_id} static"},
                    {"kind": "rpc", "handler": "instructions"},
                ],
                hooks=[
                    hook(
                        "before_model_request",
                        id="before",
                        ordering={"wraps": [f"{wraps}.before"]} if wraps else None,
                    ),
                    hook(
                        "after_model_request",
                        id="after",
                        ordering={"wraps": [f"{wraps}.after"]} if wraps else None,
                    ),
                ],
            ),
            {
                "instructions": instruction,
                "before": record(f"{plugin_id} before"),
                "after": record(f"{plugin_id} after"),
            },
        )

    right, left, other = client("right"), client("left", wraps="right"), client("other")
    discovered = await describe_plugins([right, left, other])
    clients = {"right": right, "left": left, "other": other}

    def before(label: str):
        def callback(ctx: RunContext[Any], request_context: ModelRequestContext):
            assert isinstance(ctx.prompt, str)
            traces[ctx.prompt].append(label)
            return request_context

        return callback

    def after(label: str):
        def callback(ctx: RunContext[Any], *, request_context: Any, response: ModelResponse):
            assert isinstance(ctx.prompt, str)
            traces[ctx.prompt].append(label)
            return response

        return callback

    x = Hooks(before_model_request=before("X before"), after_model_request=after("X after"))
    y = Hooks(before_model_request=before("Y before"), after_model_request=after("Y after"))
    host = PluginHostCapability[list[PluginManifest]](
        lambda ctx: ctx.deps,
        client_factory=clients.__getitem__,
        id="selected_plugins",
        ordering=CapabilityOrdering(wrapped_by=[x], wraps=[y]),
    )

    def respond(messages: list[ModelMessage], info: AgentInfo):
        return ModelResponse(parts=[TextPart(info.instructions or "empty")])

    agent = Agent(FunctionModel(respond), capabilities=[y, host, x], deps_type=list[PluginManifest])
    assert all(c.calls == [("plugin.describe", {})] for c in clients.values())
    async with agent, asyncio.timeout(5):
        first, second = await asyncio.gather(
            agent.run("pair", deps=discovered[:2]),
            agent.run("other", deps=discovered[2:]),
        )
        empty = await agent.run("empty", deps=[])
    assert first.output == "right static\n\nleft static\n\nright dynamic\n\nleft dynamic"
    assert second.output == "other static\n\nother dynamic"
    assert empty.output == "empty"
    assert traces == {
        "pair": [
            "X before",
            "left before",
            "right before",
            "Y before",
            "Y after",
            "right after",
            "left after",
            "X after",
        ],
        "other": ["X before", "other before", "Y before", "Y after", "other after", "X after"],
        "empty": ["X before", "Y before", "Y after", "X after"],
    }
    assert host.get_instructions() is None
    assert not host.has_on_event
    assert all(
        sum(method == "plugin.describe" for method, _ in c.calls) == 1 for c in clients.values()
    )


async def test_json_snapshots_rebuild_without_client_lookup_and_isolate_mutations():
    trace: list[str] = []
    first = Client(
        manifest(
            plugin_id="first",
            instructions=[{"kind": "text", "text": "First instruction"}],
            hooks=[
                hook("before_model_request", ordering={"wraps": ["second.before_model_request"]})
            ],
        ),
        {"before_model_request": lambda p: trace.append("first") or {"kind": "keep"}},
    )
    second = Client(
        manifest(plugin_id="second", hooks=[hook("before_model_request")]),
        {"before_model_request": lambda p: trace.append("second") or {"kind": "keep"}},
    )
    adapter = TypeAdapter(list[PluginManifest])
    restored = adapter.validate_json(adapter.dump_json(await describe_plugins([second, first])))
    clients = {"first": first, "second": second}
    lookups: list[str] = []

    def client_factory(plugin_id: str):
        lookups.append(plugin_id)
        return clients[plugin_id]

    hosts = [
        build_plugin_host(restored, client_factory=client_factory, id=f"snapshot_{i}")
        for i in range(2)
    ]
    with pytest.raises(PluginHostError, match="not declared"):
        await hosts[0].invoke_plugin(context(), "first", "undeclared_handler", {})
    assert lookups == []
    with pytest.raises(PluginHostError, match="duplicate plugin") as duplicate:
        build_plugin_host(restored + restored[:1], client_factory=client_factory)
    assert (duplicate.value.phase, duplicate.value.plugin_id) == ("declaration", "second")
    malformed = PluginManifest.model_construct(protocol_version=True, plugin_id="invalid")
    with pytest.raises(PluginHostError, match="invalid plugin manifest") as invalid:
        build_plugin_host([malformed], client_factory=client_factory)
    assert invalid.value.__cause__ is not None

    restored[1].instructions.clear()
    ordering = restored[1].hooks[0].ordering
    assert ordering is not None
    ordering.wraps[:] = ["missing.hook"]
    restored[1].hooks.clear()
    restored.clear()

    def respond(messages: list[ModelMessage], info: AgentInfo):
        assert info.instructions == "First instruction"
        return ModelResponse(parts=[TextPart("ok")])

    for host in hosts:
        result = await Agent(FunctionModel(respond), capabilities=[host]).run("restored snapshot")
        assert result.output == "ok"
    assert trace == lookups == ["first", "second"] * 2
    assert all(
        sum(method == "plugin.describe" for method, _ in c.calls) == 1 for c in clients.values()
    )


async def test_native_two_level_ordering_keeps_host_contiguous_and_opaque():
    trace: list[str] = []

    def record(label: str) -> Handler:
        def callback(payload: dict[str, Any]):
            trace.append(label)
            return {"kind": "keep"}

        return callback

    a = Client(
        manifest(
            plugin_id="a",
            hooks=[
                hook("before_model_request", id="before", ordering={"position": "outermost"}),
                hook("after_model_request", id="after", ordering={"wraps": ["b.after"]}),
            ],
        ),
        {"before": record("A before"), "after": record("A after")},
    )
    b = Client(
        manifest(
            plugin_id="b",
            hooks=[
                hook("before_model_request", id="before", ordering={"wrapped_by": ["a.before"]}),
                hook("after_model_request", id="after"),
            ],
        ),
        {"before": record("B before"), "after": record("B after")},
    )

    def before(label: str):
        def callback(ctx: RunContext[Any], request_context: ModelRequestContext):
            trace.append(label)
            return request_context

        return callback

    def after(label: str):
        def callback(ctx: RunContext[Any], *, request_context: Any, response: ModelResponse):
            trace.append(label)
            return response

        return callback

    x = Hooks(before_model_request=before("X before"), after_model_request=after("X after"))
    y = Hooks(before_model_request=before("Y before"), after_model_request=after("Y after"))
    host = await load_plugin_host([b, a], ordering=CapabilityOrdering(wrapped_by=[x], wraps=[y]))
    leaves = []
    host.apply(leaves.append)
    assert leaves == [host]
    assert host.visit_and_replace(lambda cap: cap) is host
    result = await Agent(TestModel(custom_output_text="ok"), capabilities=[y, host, x]).run("test")
    assert result.output == "ok"
    assert trace == [
        "X before",
        "A before",
        "B before",
        "Y before",
        "Y after",
        "B after",
        "A after",
        "X after",
    ]


async def test_native_error_chain_unhandled_then_outer_recovery():
    trace: list[str] = []
    failure = RuntimeError("model failed")

    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise failure

    def remote(label: str):
        def callback(payload: dict[str, Any]):
            trace.append(label)
            assert payload["error"] == {"type_name": "RuntimeError", "message": "model failed"}
            return {"kind": "unhandled"}

        return callback

    client = Client(
        manifest(
            hooks=[hook("on_model_request_error", id="a"), hook("on_model_request_error", id="b")]
        ),
        {"a": remote("A error"), "b": remote("B error")},
    )

    def recover(ctx: RunContext[Any], *, request_context: Any, error: Exception):
        trace.append("X error")
        assert error is failure
        return ModelResponse(parts=[TextPart("recovered")])

    x = Hooks(model_request_error=recover)
    host = await load_plugin_host([client])
    result = await Agent(FunctionModel(fail), capabilities=[x, host]).run("test")
    assert result.output == "recovered"
    assert trace == ["B error", "A error", "X error"]


async def test_sdk_tool_error_hook_receives_input_and_recovers_result():
    def fail(n: int):
        raise RuntimeError("local tool failed")

    def recover(payload: dict[str, Any]):
        assert payload["call"]["tool_name"] == NAME
        assert payload["tool"]["name"] == NAME
        assert payload["args"] == {"n": 2}
        assert payload["error"] == {"type_name": "RuntimeError", "message": "local tool failed"}
        return value({"recovered": 2})

    client = Client(
        manifest(hooks=[hook("on_tool_execute_error")]), {"on_tool_execute_error": recover}
    )
    host = await load_plugin_host([client])
    result = await Agent(
        tool_model({"n": 2}), capabilities=[host], tools=[Tool(fail, name=NAME)]
    ).run("test")
    assert json.loads(result.output) == {"recovered": 2}


@pytest.mark.parametrize(
    "point", ["after_tool_validate", "before_tool_execute", "on_tool_validate_error"]
)
async def test_replacement_or_recovery_args_are_revalidated(point: str):
    client = Client(
        manifest(hooks=[hook(point)]),
        {point: lambda p: value({"n": 0})},
    )
    host = await load_plugin_host([client])
    initial = {"n": 0} if point == "on_tool_validate_error" else {"n": 2}
    with pytest.raises(UnexpectedModelBehavior, match="exceeded max retries"):
        await Agent(tool_model(initial), capabilities=[host], tools=[add], retries=0).run("test")
    assert client.calls[-1][1]["handler"] == point


async def test_root_reference_schema_allows_valid_tool_argument_replacement():
    tool = Tool.from_schema(
        add,
        name=NAME,
        description="Add one",
        json_schema={
            "$ref": "#/$defs/args",
            "$defs": {
                "args": {
                    "type": "object",
                    "properties": {"n": {"type": "integer", "minimum": 1}},
                    "required": ["n"],
                    "additionalProperties": False,
                }
            },
        },
    )
    client = Client(
        manifest(hooks=[hook("after_tool_validate")]),
        {"after_tool_validate": lambda p: value({"n": 3})},
    )
    host = await load_plugin_host([client])
    result = await Agent(tool_model({"n": 2}), capabilities=[host], tools=[tool]).run("test")
    assert result.output == "4"


async def test_validation_recovery_and_tool_transformations_reach_agent_output():
    client = Client(
        manifest(
            hooks=[
                hook("on_tool_validate_error"),
                hook("before_tool_execute"),
                hook("after_tool_execute"),
            ],
        ),
        {
            "on_tool_validate_error": lambda p: value({"n": 4}),
            "before_tool_execute": lambda p: value({"n": p["args"]["n"] + 1}),
            "after_tool_execute": lambda p: value({"answer": p["result"]}),
        },
    )
    host = await load_plugin_host([client])
    result = await Agent(tool_model({"n": 0}), capabilities=[host], tools=[add]).run("test")
    assert json.loads(result.output) == {"answer": 6}


async def test_before_tool_validate_repairs_raw_args_before_schema_validation():
    def repair(payload: dict[str, Any]):
        assert payload["call"]["tool_name"] == NAME
        assert payload["tool"]["name"] == NAME
        assert payload["args"] == {"n": "invalid integer"}
        return value({"n": 4})

    client = Client(
        manifest(hooks=[hook("before_tool_validate")]),
        {"before_tool_validate": repair},
    )
    host = await load_plugin_host([client])
    result = await Agent(
        tool_model({"n": "invalid integer"}), capabilities=[host], tools=[add], retries=0
    ).run("test")
    assert result.output == "5"


async def test_request_replacement_preserves_local_fields_and_response_recovers():
    client = Client(
        manifest(hooks=[hook("before_model_request"), hook("on_model_request_error")]),
        {
            "before_model_request": lambda p: value(
                {"messages": p["request"]["messages"], "model_settings": {"temperature": 0.9}}
            ),
            "on_model_request_error": lambda p: value(
                {"parts": [{"part_kind": "text", "content": "recovered"}], "kind": "response"}
            ),
        },
    )
    host = await load_plugin_host([client])
    original = request()
    updated = await host.before_model_request(context(), original)
    assert updated.model is original.model
    assert updated.model_request_parameters is original.model_request_parameters
    assert (updated.model_id, updated.streaming, updated.model_settings) == (
        "local-test",
        True,
        {"temperature": 0.9},
    )

    def fail(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("boom")

    result = await Agent(FunctionModel(fail), capabilities=[host]).run("test")
    assert result.output == "recovered"


async def test_model_request_echo_preserves_provider_settings():
    class ProviderSettings(ModelSettings, total=False):
        openai_reasoning_effort: str

    settings = ProviderSettings(temperature=0.3, openai_reasoning_effort="high")
    client = Client(
        manifest(hooks=[hook("before_model_request")]),
        {"before_model_request": lambda p: value(p["request"])},
    )
    host = await load_plugin_host([client])

    def respond(messages: list[ModelMessage], info: AgentInfo):
        assert info.model_settings == settings
        return ModelResponse(parts=[TextPart("provider settings preserved")])

    result = await Agent(FunctionModel(respond), capabilities=[host], model_settings=settings).run(
        "test"
    )
    assert result.output == "provider settings preserved"


@pytest.mark.parametrize("modify", ["filter", "description", "schema", "rename", "duplicate"])
async def test_prepare_tools_only_filters_or_changes_description(modify: str):
    def prepare(payload: dict[str, Any]):
        definitions = payload["tools"]
        if modify == "filter":
            return value([])
        if modify == "description":
            definitions[0]["description"] = "Updated description"
        elif modify == "schema":
            definitions[0]["parameters_json_schema"] = {"type": "object"}
        elif modify == "rename":
            definitions[0]["name"] = "renamed"
        else:
            definitions += deepcopy(definitions)
        return value(definitions)

    client = Client(manifest(hooks=[hook("prepare_tools")]), {"prepare_tools": prepare})
    host = await load_plugin_host([client])
    from pydantic_ai.tools import ToolDefinition

    definitions = [ToolDefinition(name=NAME, description="original", parameters_json_schema=SCHEMA)]
    if modify in ("schema", "rename", "duplicate"):
        with pytest.raises(PluginHostError, match="only filter tools"):
            await host.prepare_tools(context(), definitions)
    else:
        result = await host.prepare_tools(context(), definitions)
        assert result == (
            []
            if modify == "filter"
            else [replace(definitions[0], description="Updated description")]
        )


async def test_structured_output_hooks_prepare_description_and_repair_raw_output():
    class Answer(BaseModel):
        answer: int

    def prepare(payload: dict[str, Any]):
        assert len(payload["tools"]) == 1
        payload["tools"][0]["description"] = "Plugin-prepared structured answer"
        return value(payload["tools"])

    def repair(payload: dict[str, Any]):
        assert payload == {"output": {"answer": "invalid integer"}}
        return value({"answer": 42})

    client = Client(
        manifest(hooks=[hook("prepare_output_tools"), hook("before_output_validate")]),
        {"prepare_output_tools": prepare, "before_output_validate": repair},
    )
    host = await load_plugin_host([client])

    def respond(messages: list[ModelMessage], info: AgentInfo):
        assert info.output_tools[0].description == "Plugin-prepared structured answer"
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"answer": "invalid integer"})]
        )

    result = await Agent(
        FunctionModel(respond), output_type=Answer, capabilities=[host], retries=0
    ).run("test")
    assert result.output == Answer(answer=42)


@pytest.mark.parametrize("point", ["before_run", "on_event"])
async def test_observation_hooks_receive_data_and_require_null(point: str):
    client = Client(manifest(hooks=[hook(point)]), {point: lambda p: value(None)})
    host = await load_plugin_host([client])
    result = await Agent(TestModel(custom_output_text="ok"), capabilities=[host]).run("test")
    assert result.output == "ok"
    payloads = [p["payload"] for method, p in client.calls if method == "plugin.invoke"]
    if point == "before_run":
        assert payloads == [{}]
    else:
        assert payloads
        assert all(isinstance(p["event"]["event_kind"], str) for p in payloads)
    client.handlers[point] = lambda p: {"kind": "keep"}
    with pytest.raises(PluginHostError, match="observation hooks"):
        await Agent(TestModel(custom_output_text="ok"), capabilities=[host]).run("test")


async def test_selected_event_hook_observes_custom_event_defined_after_discovery():
    client = Client(manifest(hooks=[hook("on_event")]), {"on_event": lambda p: value(None)})
    discovered = await describe_plugins([client])
    host = PluginHostCapability[list[PluginManifest]](
        lambda ctx: ctx.deps, client_factory=lambda plugin_id: client
    )

    @dataclass(kw_only=True)
    class PluginHostLateProgress(CustomEvent):
        done: int

    agent = Agent(TestModel(), capabilities=[host], deps_type=list[PluginManifest])

    @agent.tool
    async def progress(ctx: RunContext[list[PluginManifest]]) -> str:
        await ctx.emit(PluginHostLateProgress(done=1))
        return "done"

    await agent.run("report progress", deps=discovered)
    events = [
        params["payload"]["event"]
        for method, params in client.calls
        if method == "plugin.invoke" and params["payload"]["event"]["event_kind"] == "custom"
    ]
    assert len(events) == 1
    assert events[0]["name"] == "plugin_host_late_progress"
    assert events[0]["done"] == 1
    assert events[0]["tool_name"] == "progress"


async def test_keep_preserves_local_object_identity():
    client = Client(
        manifest(hooks=[hook("before_model_request"), hook("after_model_request")]),
        {
            "before_model_request": lambda p: {"kind": "keep"},
            "after_model_request": lambda p: {"kind": "keep"},
        },
    )
    host = await load_plugin_host([client])
    original = request()
    response = ModelResponse(parts=[TextPart("ok")])
    assert await host.before_model_request(context(), original) is original
    assert (
        await host.after_model_request(context(), request_context=original, response=response)
        is response
    )


@pytest.mark.parametrize("data", [object(), (1, 2), {"n": float("nan")}, {"n": float("inf")}])
async def test_non_json_tool_results_are_rejected_before_hook_rpc(data: Any):
    client = Client(
        manifest(hooks=[hook("after_tool_execute")]), {"after_tool_execute": lambda p: value(None)}
    )
    host = await load_plugin_host([client])
    from pydantic_ai.tools import ToolDefinition

    with pytest.raises(PluginHostError, match="not JSON-native"):
        await host.after_tool_execute(
            context(),
            call=ToolCallPart("local", {}),
            tool_def=ToolDefinition(name="local"),
            args={},
            result=data,
        )
    assert client.calls == [("plugin.describe", {})]


@pytest.mark.parametrize(
    "reply",
    [
        value(float("nan")),
        {"kind": "unknown"},
        {"kind": "value", "value": "x", "extra": True},
        {"kind": "keep"},
    ],
)
async def test_instruction_reply_errors_include_export_identity(reply: Any):
    client = Client(
        manifest(instructions=[{"kind": "rpc", "handler": "instructions"}]),
        {"instructions": lambda p: reply},
    )
    host = await load_plugin_host([client])
    with pytest.raises(PluginHostError) as caught:
        await Agent(TestModel(), capabilities=[host]).run("test")
    assert (caught.value.phase, caught.value.plugin_id, caught.value.handler) == (
        "data",
        "demo",
        "instructions",
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"protocol_version": True},
        {"protocol_version": 2, "plugin_id": "known_demo"},
        {"unexpected": True},
        {"hooks": [hook("wrap_model_request")]},
        {"hooks": [hook("before_run", ordering={"wraps": ["other.hook"]})]},
        {"hooks": [hook("before_run"), hook("before_run")]},
    ],
)
async def test_invalid_declarations_fail_during_load(changes: dict[str, Any]):
    with pytest.raises(PluginHostError) as caught:
        await load_plugin_host([Client(manifest(**changes))])
    assert caught.value.phase == "declaration"
    assert caught.value.plugin_id == changes.get("plugin_id", "demo")


async def test_duplicate_plugins_and_native_cycles_are_load_errors():
    with pytest.raises(PluginHostError, match="duplicate plugin"):
        await load_plugin_host([Client(manifest()), Client(manifest())])
    cyclic = manifest(
        hooks=[
            hook("before_run", id="a", ordering={"wraps": ["demo.b"]}),
            hook("before_run", id="b", ordering={"wraps": ["demo.a"]}),
        ]
    )
    with pytest.raises(UserError, match="Circular ordering"):
        await load_plugin_host([Client(cyclic)])


@pytest.mark.parametrize("self_ref", ["#", ""])
async def test_argument_hook_preserves_recursive_schema_and_literal_data(self_ref: str):
    literal = {"$ref": "https://example.invalid/schema"}
    schema = {
        "type": "object",
        "properties": {
            "n": {"type": "integer"},
            "child": {"$ref": self_ref},
            "literal": {"const": literal},
        },
        "required": ["n"],
        "examples": [literal],
        "default": literal,
    }
    replacement = {"n": 2, "child": {"n": 3}, "literal": literal}
    client = Client(
        manifest(hooks=[hook("after_tool_validate")]),
        {"after_tool_validate": lambda p: value(replacement)},
    )
    host = await load_plugin_host([client])
    from pydantic_ai.tools import ToolDefinition

    result = await host.after_tool_validate(
        context(),
        call=ToolCallPart(NAME, {}),
        tool_def=ToolDefinition(name=NAME, parameters_json_schema=schema),
        args={},
    )
    assert result == replacement


@pytest.mark.parametrize(
    ("ref", "message"),
    [
        ("https://example.invalid/schema", "only document-internal schema references"),
        ("#/$defs/missing", "unresolvable document-internal schema reference"),
    ],
)
async def test_argument_hook_rejects_unsafe_or_unresolvable_schema_references(
    ref: str, message: str
):
    client = Client(
        manifest(hooks=[hook("after_tool_validate")]),
        {"after_tool_validate": lambda p: value({"n": 2})},
    )
    host = await load_plugin_host([client])
    from pydantic_ai.tools import ToolDefinition

    with pytest.raises(ValueError, match=message):
        await host.after_tool_validate(
            context(),
            call=ToolCallPart(NAME, {"n": 1}),
            tool_def=ToolDefinition(
                name=NAME,
                parameters_json_schema={
                    "type": "object",
                    "properties": {"n": {"$ref": ref}},
                    "required": ["n"],
                },
            ),
            args={"n": 1},
        )


@pytest.mark.parametrize("rpc_method", ["plugin.describe", "plugin.invoke"])
async def test_rpc_failure_cause_and_cancellation_propagate(rpc_method: str):
    failure = OSError("transport unavailable")

    def fail(payload: dict[str, Any]):
        raise failure

    handler: Handler = fail

    class FailingClient(Client):
        async def call(self, method: str, params: dict[str, Any]) -> Any:
            if method == rpc_method:
                result = handler(params)
                return await result if inspect.isawaitable(result) else result
            return await super().call(method, params)

    client = FailingClient(manifest(hooks=[hook("before_run")]))
    host = await load_plugin_host([client]) if rpc_method == "plugin.invoke" else None

    async def operation():
        if host is None:
            await describe_plugins([client])
        else:
            await host.before_run(context())

    with pytest.raises(PluginHostError) as caught:
        await operation()
    assert caught.value.__cause__ is failure
    assert (
        caught.value.phase,
        caught.value.plugin_id,
        caught.value.handler,
        caught.value.point,
    ) == (
        ("rpc", "demo", "before_run", "before_run")
        if rpc_method == "plugin.invoke"
        else ("rpc", None, None, None)
    )

    entered = asyncio.Event()
    cancelled = asyncio.Event()

    async def block(payload: dict[str, Any]):
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    handler = block
    task = asyncio.create_task(operation())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()

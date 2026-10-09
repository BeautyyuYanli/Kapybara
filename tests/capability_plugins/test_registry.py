"""Exercise plugin input and composition through actual SDK Agent runs."""

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, RootModel, ValidationError, model_validator
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.agent.abstract import AbstractAgent
from pydantic_ai.capabilities import AbstractCapability, Capability, CombinedCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests, ToolDefinition

from capability_plugins.deferred import build_lazy, lazy_model
from capability_plugins.tool_definitions import build_approval
from kapy.capability_plugins import PluginDeps, PluginRegistry, PluginSpec
from kapy.capability_plugins.examples.echo import EchoConfig, build_echo
from kapy.capability_plugins.examples.new_plugin import MyConfig, build_my_plugin


async def echo_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returns = [
        part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)
    ]
    if returns:
        return ModelResponse(parts=[TextPart(str(returns[-1].content))])
    if info.function_tools:
        assert [tool.name for tool in info.function_tools] == ["example_echo"]
        assert "需要回显文本" in (info.instructions or "")
        return ModelResponse(parts=[ToolCallPart("example_echo", {"text": "hello"})])
    assert not info.instructions
    return ModelResponse(parts=[TextPart("disabled")])


def echo_registry() -> PluginRegistry[PluginDeps]:
    registry = PluginRegistry[PluginDeps]()
    registry.register("echo", config_type=EchoConfig, factory=build_echo)
    return registry


def test_dto_round_trip_and_validation() -> None:
    deps = PluginDeps(
        plugins=(
            PluginSpec(plugin_name="echo", config={"prefix": "A:", "uppercase": True}),
            PluginSpec(plugin_name="null", config=None),
        )
    )
    assert PluginDeps.model_validate_json(deps.model_dump_json()) == deps
    assert PluginSpec(plugin_name="echo").config == {}
    for value in (
        {"plugin_name": ""},
        {"plugin_name": "echo", "plugin_provider": "legacy"},
        {"plugin_name": "echo", "config": object()},
    ):
        with pytest.raises(ValidationError):
            PluginSpec.model_validate(value)


def test_registration_rejects_empty_and_duplicate_names() -> None:
    registry = echo_registry()
    for name in ("", "echo"):
        with pytest.raises(ValueError):
            registry.register(name, config_type=EchoConfig, factory=build_echo)


@pytest.mark.asyncio
async def test_same_agent_reconfigures_and_disables_plugins_concurrently() -> None:
    agent = Agent(
        FunctionModel(echo_model), deps_type=PluginDeps, capabilities=[echo_registry().capability()]
    )
    inputs = [
        PluginDeps(plugins=(PluginSpec(plugin_name="echo", config={"prefix": "A:"}),)),
        PluginDeps(
            plugins=(PluginSpec(plugin_name="echo", config={"prefix": "B:", "uppercase": True}),)
        ),
        PluginDeps(),
    ]
    results = await asyncio.gather(*(agent.run("hello", deps=deps) for deps in inputs))
    assert [result.output for result in results] == ["A:hello", "B:HELLO", "disabled"]
    assert (await agent.run("hello", deps=inputs[0])).output == "A:hello"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("specs", "message"),
    [
        ((PluginSpec(plugin_name="unknown"),), "not registered"),
        ((PluginSpec(plugin_name="echo"), PluginSpec(plugin_name="echo")), "more than once"),
        ((PluginSpec(plugin_name="echo", config={"unknown": True}),), "Invalid configuration"),
        ((PluginSpec(plugin_name="echo", config=None),), "Invalid configuration"),
    ],
)
async def test_invalid_selection_fails_as_sdk_user_error(
    specs: tuple[PluginSpec, ...], message: str
) -> None:
    agent = Agent(TestModel(), deps_type=PluginDeps, capabilities=[echo_registry().capability()])
    with pytest.raises(UserError, match=message):
        await agent.run("hello", deps=PluginDeps(plugins=specs))


@pytest.mark.asyncio
async def test_entrance_captures_registration_snapshot() -> None:
    registry = echo_registry()
    old_agent = Agent(TestModel(), deps_type=PluginDeps, capabilities=[registry.capability()])
    registry.register("my_plugin", config_type=MyConfig, factory=build_my_plugin)
    deps = PluginDeps(
        plugins=(PluginSpec(plugin_name="my_plugin", config={"instructions": "new"}),)
    )
    with pytest.raises(UserError, match="not registered"):
        await old_agent.run("hello", deps=deps)
    new_agent = Agent(TestModel(), deps_type=PluginDeps, capabilities=[registry.capability()])
    assert (await new_agent.run("hello", deps=deps)).output == "success (no tool calls)"


@pytest.mark.asyncio
async def test_root_model_accepts_json_null() -> None:
    def build(config: RootModel[None]) -> AbstractCapability[PluginDeps]:
        assert config.root is None
        return Capability(id="null", instructions="null config")

    registry = PluginRegistry[PluginDeps]()
    registry.register("null", config_type=RootModel[None], factory=build)
    agent = Agent(TestModel(), deps_type=PluginDeps, capabilities=[registry.capability()])
    await agent.run(
        "hello", deps=PluginDeps(plugins=(PluginSpec(plugin_name="null", config=None),))
    )


@pytest.mark.asyncio
async def test_config_copy_protects_input_from_custom_validation() -> None:
    class Config(BaseModel):
        items: list[str]

        @model_validator(mode="before")
        @classmethod
        def append_item(cls, value: Any) -> Any:
            value["items"].append("validated")
            return value

    def build(config: Config) -> AbstractCapability[PluginDeps]:
        return Capability(id="copy", instructions=",".join(config.items))

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(info.instructions or "")])

    registry = PluginRegistry[PluginDeps]()
    registry.register("copy", config_type=Config, factory=build)
    agent = Agent(FunctionModel(model), deps_type=PluginDeps, capabilities=[registry.capability()])
    spec = PluginSpec(plugin_name="copy", config={"items": ["input"]})
    for _ in range(2):
        assert (
            await agent.run("hello", deps=PluginDeps(plugins=(spec,)))
        ).output == "input,validated"
        assert spec.config == {"items": ["input"]}


@pytest.mark.asyncio
async def test_factory_return_validation_and_exception_propagation() -> None:
    bad_factory = cast(
        Callable[[EchoConfig], AbstractCapability[PluginDeps]], lambda config: "invalid"
    )
    registry = PluginRegistry[PluginDeps]()
    registry.register("bad", config_type=EchoConfig, factory=bad_factory)

    def broken(config: EchoConfig) -> AbstractCapability[PluginDeps]:
        raise RuntimeError("factory bug")

    registry.register("broken", config_type=EchoConfig, factory=broken)
    agent = Agent(TestModel(), deps_type=PluginDeps, capabilities=[registry.capability()])
    with pytest.raises(UserError, match="must return AbstractCapability"):
        await agent.run("hello", deps=PluginDeps(plugins=(PluginSpec(plugin_name="bad"),)))
    with pytest.raises(RuntimeError, match="factory bug"):
        await agent.run("hello", deps=PluginDeps(plugins=(PluginSpec(plugin_name="broken"),)))


@pytest.mark.asyncio
async def test_tool_names_are_not_rewritten_and_sdk_reports_conflicts() -> None:
    registry = PluginRegistry[PluginDeps]()
    registry.register("one", config_type=EchoConfig, factory=build_echo)

    def build_second(config: EchoConfig) -> AbstractCapability[PluginDeps]:
        def example_echo(text: str) -> str:
            return text

        return Capability(id="second", tools=[example_echo])

    registry.register("two", config_type=EchoConfig, factory=build_second)
    agent = Agent(TestModel(), deps_type=PluginDeps, capabilities=[registry.capability()])
    with pytest.raises(UserError, match="example_echo"):
        await agent.run(
            "hello",
            deps=PluginDeps(plugins=(PluginSpec(plugin_name="one"), PluginSpec(plugin_name="two"))),
        )


@pytest.mark.asyncio
async def test_tool_definitions_preserve_approval_metadata_and_declared_ownership() -> None:
    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        (definition,) = info.function_tools
        assert definition.name == "approved_echo"
        assert definition.kind == "unapproved"
        assert definition.metadata == {"category": "example"}
        assert definition.strict is True and definition.sequential is True
        assert definition.capability_id == "declared_owner"
        return ModelResponse(parts=[TextPart("checked")])

    registry = PluginRegistry[PluginDeps]()
    registry.register("approval", config_type=EchoConfig, factory=build_approval)
    agent = Agent(
        FunctionModel(model),
        deps_type=PluginDeps,
        output_type=str | DeferredToolRequests,
        capabilities=[registry.capability()],
    )
    assert (
        await agent.run("hello", deps=PluginDeps(plugins=(PluginSpec(plugin_name="approval"),)))
    ).output == "checked"


@pytest.mark.asyncio
async def test_tool_hooks_compose_once_for_each_call_with_correct_ownership() -> None:
    class HookCapability(Capability[PluginDeps]):
        def __init__(self, config: EchoConfig) -> None:
            self.marker = config.prefix

            def echo(text: str) -> str:
                return f"{self.marker}:{text}"

            super().__init__(id=self.marker, tools=[Tool(echo, name=f"{self.marker}_echo")])

        async def after_tool_execute(
            self,
            ctx: RunContext[PluginDeps],
            *,
            call: ToolCallPart,
            tool_def: ToolDefinition,
            args: dict[str, Any],
            result: Any,
        ) -> Any:
            assert ctx.capability_active is True
            assert tool_def.capability_id == call.tool_name.removesuffix("_echo")
            return f"{result}|{self.marker}"

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(
                parts=[ToolCallPart(tool.name, {"text": "hello"}) for tool in info.function_tools]
            )
        return ModelResponse(
            parts=[TextPart(json.dumps({part.tool_name: part.content for part in returns}))]
        )

    registry = PluginRegistry[PluginDeps]()
    registry.register("a", config_type=EchoConfig, factory=HookCapability)
    registry.register("b", config_type=EchoConfig, factory=HookCapability)
    agent = Agent(FunctionModel(model), deps_type=PluginDeps, capabilities=[registry.capability()])
    result = await agent.run(
        "hello",
        deps=PluginDeps(
            plugins=(
                PluginSpec(plugin_name="a", config={"prefix": "A"}),
                PluginSpec(plugin_name="b", config={"prefix": "B"}),
            )
        ),
    )
    assert json.loads(result.output) == {"A_echo": "A:hello|B|A", "B_echo": "B:hello|B|A"}


@pytest.mark.asyncio
async def test_in_process_keeps_sdk_anonymous_tool_capability_identity() -> None:
    def build(config: EchoConfig) -> AbstractCapability[PluginDeps]:
        def echo(text: str) -> str:
            return text

        return Capability(tools=[echo])

    registry = PluginRegistry[PluginDeps]()
    registry.register("anonymous", config_type=EchoConfig, factory=build)
    agent = Agent(TestModel(), deps_type=PluginDeps, capabilities=[registry.capability()])
    # In-process runs keep the SDK's anonymous capability identity; durable
    # reconstruction instead requires the plugin's explicit stable ID.
    await agent.run("hello", deps=PluginDeps(plugins=(PluginSpec(plugin_name="anonymous"),)))


@pytest.mark.asyncio
async def test_deferred_loading_keeps_native_capability_and_toolset_instructions() -> None:
    registry = PluginRegistry[PluginDeps]()
    registry.register("lazy", config_type=EchoConfig, factory=build_lazy)
    agent = Agent(
        FunctionModel(lazy_model), deps_type=PluginDeps, capabilities=[registry.capability()]
    )
    result = await agent.run(
        "load lazy", deps=PluginDeps(plugins=(PluginSpec(plugin_name="lazy"),))
    )
    assert json.loads(result.output) == {
        "instructions": {"instructions": "CAPABILITY_RULE\n\nTOOLSET_RULE"},
        "tool_result": "lazy-result",
    }


@pytest.mark.asyncio
async def test_combines_nested_capabilities_hooks_and_consumer_deps() -> None:
    class ConsumerDeps(PluginDeps):
        request_id: str

    class Config(BaseModel):
        model_config = ConfigDict(extra="forbid")
        label: str

    instances: list[AbstractCapability[ConsumerDeps]] = []
    bound: list[tuple[str, str]] = []

    class HookCapability(Capability[ConsumerDeps]):
        def __init__(self, label: str) -> None:
            self.label = label
            super().__init__(id=label, instructions=label)

        def for_agent(
            self, agent: AbstractAgent[ConsumerDeps, Any]
        ) -> AbstractCapability[ConsumerDeps]:
            bound.append((self.label, "agent"))
            return self

        async def for_run(self, ctx: RunContext[ConsumerDeps]) -> AbstractCapability[ConsumerDeps]:
            bound.append((self.label, ctx.deps.request_id))
            return self

        async def after_model_request(
            self,
            ctx: RunContext[ConsumerDeps],
            *,
            request_context: ModelRequestContext,
            response: ModelResponse,
        ) -> ModelResponse:
            return replace(
                response, parts=[TextPart(f"{response.text}|{self.label}:{ctx.deps.request_id}")]
            )

    def build(config: Config) -> AbstractCapability[ConsumerDeps]:
        capability = HookCapability(config.label)
        instances.append(capability)
        return CombinedCapability(capabilities=[capability])

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert info.instructions is not None
        assert info.instructions.index("A") < info.instructions.index("B")
        return ModelResponse(parts=[TextPart("done")])

    registry = PluginRegistry[ConsumerDeps]()
    registry.register("first", config_type=Config, factory=build)
    registry.register("second", config_type=Config, factory=build)
    agent = Agent(
        FunctionModel(model), deps_type=ConsumerDeps, capabilities=[registry.capability()]
    )
    for request_id in ("one", "two"):
        result = await agent.run(
            "hello",
            deps=ConsumerDeps(
                request_id=request_id,
                plugins=(
                    PluginSpec(plugin_name="first", config={"label": "A"}),
                    PluginSpec(plugin_name="second", config={"label": "B"}),
                ),
            ),
        )
        assert result.output == f"done|B:{request_id}|A:{request_id}"
    assert len({id(instance) for instance in instances}) == 4
    assert sorted(bound) == sorted(
        (label, stage) for label in ("A", "B") for stage in ("agent", "one", "agent", "two")
    )

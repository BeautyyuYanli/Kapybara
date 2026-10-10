"""Static factory contracts and writable JSON access through native SDK runs."""

import asyncio
import json
from dataclasses import replace
from typing import Any, Self
from uuid import UUID

import pytest
from pydantic import BaseModel, RootModel, ValidationError, model_validator
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import Capability, CombinedCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import FunctionToolset, WrapperToolset

from kapy.layers import (
    Layer,
    LayerAccess,
    LayerData,
    LayerDeps,
    LayerFactory,
    layers_capability,
    load_layers,
    make_layers,
    snapshot_layers,
)
from kapy.layers.examples import echo, new_layer
from kapy.layers.modules import dump_object
from kapy.layers.types import JsonObject
from layers import deferred, tool_definitions


def get_layers(deps: LayerDeps) -> dict[str, LayerData]:
    return deps.layers


ECHO = make_layers({"echo": echo.make}, get_layers)


async def echo_model(messages, info):
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


def test_json_loading_restores_defaults_copies_data_and_requires_explicit_writeback():
    configs: dict[str, JsonObject] = {"echo": {"prefix": "A:"}}
    states: dict[str, JsonObject] = {"echo": {"calls": 3}}
    deps = LayerDeps(layers=load_layers(configs, states, ECHO))
    assert LayerDeps.model_validate_json(deps.model_dump_json()) == deps
    configs["echo"]["prefix"] = "changed"
    states["echo"]["calls"] = 10
    validated = echo.State.model_validate(deps.layers["echo"]["state"])
    validated.calls += 1
    assert snapshot_layers(deps, ECHO, get_layers) == {"echo": {"calls": 3}}
    deps.layers["echo"]["state"] = dump_object(validated)
    snapshot = snapshot_layers(deps, ECHO, get_layers)
    assert snapshot == {"echo": {"calls": 4}}
    snapshot["echo"]["calls"] = 20
    assert deps.layers["echo"]["state"] == {"calls": 4}
    assert deps.layers["echo"]["config"] == {"prefix": "A:", "uppercase": False}
    assert load_layers({"echo": {}}, {}, ECHO)["echo"]["state"] == {"calls": 0}


@pytest.mark.parametrize("bad", ["result", "config", "state", "capability", "enter", "exit"])
def test_factory_rejects_invalid_definitions_at_assembly(bad):
    def factory(layer_id: str, access: LayerAccess[LayerDeps]) -> Any:
        layer = echo.make(layer_id, access)
        return {
            "result": object(),
            "config": replace(layer, config_type=dict),  # pyrefly: ignore[bad-argument-type]
            "state": replace(layer, state_type=dict),  # pyrefly: ignore[bad-argument-type]
            "capability": replace(layer, capability=FunctionToolset()),  # pyrefly: ignore[bad-argument-type]
            "enter": replace(layer, enter_scope=lambda scope, config: echo.State()),  # pyrefly: ignore[bad-argument-type]
            "exit": replace(layer, exit_scope=lambda scope, config, state: None),  # pyrefly: ignore[bad-argument-type]
        }[bad]

    with pytest.raises(UserError):
        make_layers({"invalid": factory}, get_layers)


@pytest.mark.parametrize("actual_id", [None, "", "other"])
def test_root_id_mismatch_reports_expected_and_actual_without_rewriting(actual_id):
    capability = Capability[LayerDeps](id=actual_id)

    def factory(
        layer_id: str, access: LayerAccess[LayerDeps]
    ) -> Layer[LayerDeps, echo.Config, echo.State]:
        return Layer(config_type=echo.Config, state_type=echo.State, capability=capability)

    with pytest.raises(UserError, match=f"'expected'.*{actual_id!r}"):
        make_layers({"expected": factory}, get_layers)
    assert capability.id == actual_id


def test_empty_installed_name_is_rejected_before_factory_call():
    def factory(layer_id: str, access: LayerAccess[LayerDeps]):
        pytest.fail("An empty installation id must not reach a factory")

    with pytest.raises(UserError, match="nonempty"):
        make_layers({"": factory}, get_layers)


def test_unknown_names_invalid_json_and_nonobject_models_fail():
    with pytest.raises(UserError, match="Unknown registered layer"):
        load_layers({"unknown": {}}, {}, ECHO)
    with pytest.raises(UserError, match="unconfigured"):
        load_layers({}, {"echo": {}}, ECHO)
    invalid: list[tuple[dict[str, JsonObject], dict[str, JsonObject]]] = [
        ({"echo": {"unknown": True}}, {}),
        ({"echo": {}}, {"echo": {"calls": []}}),
    ]
    for configs, states in invalid:
        with pytest.raises(ValidationError):
            load_layers(configs, states, ECHO)

    def make_null(
        layer_id: str, access: LayerAccess[LayerDeps]
    ) -> Layer[LayerDeps, RootModel[None], echo.State]:
        return Layer(
            config_type=RootModel[None],
            state_type=echo.State,
            capability=Capability(id=layer_id),
        )

    nonobject = make_layers({"null": make_null}, get_layers)
    with pytest.raises(UserError, match="JSON objects"):
        load_layers({"null": None}, {}, nonobject)  # pyrefly: ignore[bad-assignment]
    with pytest.raises(UserError, match="Unknown registered layer"):
        snapshot_layers(
            LayerDeps(layers={"unknown": {"config": {}, "state": {}}}), ECHO, get_layers
        )


def test_load_copies_inputs_before_custom_validators():
    class Config(BaseModel):
        items: list[str]

        @model_validator(mode="before")
        @classmethod
        def append(cls, value):
            value["items"].append("validated")
            return value

    def make_copy(
        layer_id: str, access: LayerAccess[LayerDeps]
    ) -> Layer[LayerDeps, Config, echo.State]:
        return Layer(
            config_type=Config,
            state_type=echo.State,
            capability=Capability(id=layer_id),
        )

    layers = make_layers({"copy": make_copy}, get_layers)
    configs: dict[str, JsonObject] = {"copy": {"items": ["input"]}}
    loaded = load_layers(configs, {}, layers)
    assert configs == {"copy": {"items": ["input"]}}
    assert loaded["copy"]["config"] == {"items": ["input", "validated"]}


@pytest.mark.asyncio
async def test_same_agent_reconfigures_disables_and_keeps_json_state_per_run():
    agent = Agent(
        FunctionModel(echo_model),
        deps_type=LayerDeps,
        capabilities=[layers_capability(ECHO, get_layers=get_layers)],
    )
    inputs = [
        LayerDeps(layers=load_layers({"echo": {"prefix": "A:"}}, {}, ECHO)),
        LayerDeps(layers=load_layers({"echo": {"prefix": "B:", "uppercase": True}}, {}, ECHO)),
        LayerDeps(),
    ]
    results = await asyncio.gather(*(agent.run("hello", deps=deps) for deps in inputs))
    assert [result.output for result in results] == ["A:hello", "B:HELLO", "disabled"]
    assert snapshot_layers(inputs[0], ECHO, get_layers) == {"echo": {"calls": 1}}
    assert snapshot_layers(inputs[1], ECHO, get_layers) == {"echo": {"calls": 1}}
    assert (await agent.run("hello", deps=inputs[0])).output == "A:hello"
    assert snapshot_layers(inputs[0], ECHO, get_layers) == {"echo": {"calls": 2}}


@pytest.mark.asyncio
async def test_factory_assembly_binds_independent_writable_access_for_arbitrary_deps():
    class Deps(BaseModel):
        payload: dict[str, LayerData]

    class Config(BaseModel):
        increment: int  # Assembly must not instantiate required models.

    class State(BaseModel):
        calls: int

    constructed = []

    async def enter(scope_id: UUID, config: Config) -> State:
        pytest.fail("Static data operations must not initialize business resources")

    async def exit(scope_id: UUID, config: Config, state: State | None) -> None:
        pytest.fail("Static data operations must not close business resources")

    def make(layer_id: str, access: LayerAccess[Deps]) -> Layer[Deps, Config, State]:
        constructed.append(layer_id)

        def count(ctx: RunContext[Deps]) -> int:
            data = access(ctx.deps)
            state = State.model_validate(data["state"])
            state.calls += Config.model_validate(data["config"]).increment
            access(ctx.deps)["state"] = dump_object(state)
            return state.calls

        return Layer(
            config_type=Config,
            state_type=State,
            capability=Capability(id=layer_id, tools=[Tool(count, name=f"{layer_id}_count")]),
            enter_scope=enter,
            exit_scope=exit,
        )

    def access(deps: Deps) -> dict[str, LayerData]:
        return deps.payload

    layers = make_layers({"one": make, "two": make}, access)
    with pytest.raises(ValidationError):
        load_layers({"one": {"increment": 1}}, {}, layers)
    deps = Deps(
        payload=load_layers(
            {"one": {"increment": 1}, "two": {"increment": 10}},
            {"one": {"calls": 2}, "two": {"calls": 3}},
            layers,
        )
    )
    agent = Agent(
        TestModel(), deps_type=Deps, capabilities=[layers_capability(layers, get_layers=access)]
    )
    assert json.loads((await agent.run("count", deps=deps)).output) == {
        "one_count": 3,
        "two_count": 13,
    }
    assert snapshot_layers(deps, layers, access) == {"one": {"calls": 3}, "two": {"calls": 13}}
    del deps.payload["two"]
    assert json.loads((await agent.run("count", deps=deps)).output) == {"one_count": 4}
    assert constructed == ["one", "two"]


@pytest.mark.asyncio
@pytest.mark.parametrize("as_layer", [False, True], ids=["native", "layer"])
async def test_sdk_for_run_sees_finalized_capabilities_and_isolates_shared_toolsets(as_layer):
    started, ready, closed, bindings = [], asyncio.Event(), [], []

    class Toolset(FunctionToolset[LayerDeps]):
        def __init__(self, access: LayerAccess[LayerDeps]):
            self.access, self.resource_label, self.entered = access, "", False

            async def identify() -> str:
                assert self.entered
                started.append(self.resource_label)
                if len(started) == 2:
                    ready.set()
                await asyncio.wait_for(ready.wait(), 5)
                return self.resource_label

            super().__init__(id="isolated-tools", tools=[identify])

        async def for_run(self, ctx: RunContext[LayerDeps]):
            assert ctx.capabilities["isolated"].id == "isolated"
            assert ctx.capabilities["peer"].id == "peer"
            toolset = Toolset(self.access)
            toolset.resource_label = echo.Config.model_validate(
                self.access(ctx.deps)["config"]
            ).prefix
            bindings.append(toolset.resource_label)
            return toolset

        async def __aenter__(self) -> Self:
            self.entered = True
            await super().__aenter__()
            return self

        async def __aexit__(self, *args) -> None:
            try:
                await super().__aexit__(*args)
            finally:
                closed.append(self.resource_label)
                self.entered = False

    def make(
        layer_id: str, access: LayerAccess[LayerDeps]
    ) -> Layer[LayerDeps, echo.Config, echo.State]:
        return Layer(
            config_type=echo.Config,
            state_type=echo.State,
            capability=Capability(id=layer_id, toolsets=[Toolset(access)]),
        )

    layers = make_layers({"isolated": make}, get_layers)
    contribution = (
        layers_capability(layers, get_layers=get_layers)
        if as_layer
        else layers["isolated"].capability
    )
    agent = Agent(
        TestModel(),
        deps_type=LayerDeps,
        capabilities=[contribution, Capability(id="peer")],
    )
    results = await asyncio.gather(
        *(
            agent.run(
                "identify",
                deps=LayerDeps(layers=load_layers({"isolated": {"prefix": label}}, {}, layers)),
            )
            for label in ("A", "B")
        )
    )
    assert [json.loads(result.output) for result in results] == [
        {"identify": "A"},
        {"identify": "B"},
    ]
    assert sorted(started) == sorted(closed) == sorted(bindings) == ["A", "B"]


@pytest.mark.asyncio
async def test_entrance_captures_static_configuration_and_rejects_bad_run_input():
    registry: dict[str, LayerFactory[LayerDeps]] = {"echo": echo.make}
    layers = make_layers(registry, get_layers)
    agent = Agent(
        TestModel(),
        deps_type=LayerDeps,
        capabilities=[layers_capability(layers, get_layers=get_layers)],
    )
    registry["new"] = new_layer.make
    for name, config in (("new", {"instructions": "new"}), ("echo", {"extra": True})):
        with pytest.raises(UserError):
            await agent.run("hello", deps=LayerDeps(layers={name: {"config": config, "state": {}}}))
    updated_layers = make_layers(registry, get_layers)
    updated = Agent(
        TestModel(),
        deps_type=LayerDeps,
        capabilities=[layers_capability(updated_layers, get_layers=get_layers)],
    )
    assert (
        await updated.run(
            "hello",
            deps=LayerDeps(
                layers=load_layers({"new": {"instructions": "new"}}, {}, updated_layers)
            ),
        )
    ).output == "success (no tool calls)"


@pytest.mark.asyncio
async def test_tool_name_conflicts_use_native_sdk():
    layers = make_layers({"one": echo.make, "two": echo.make}, get_layers)
    agent = Agent(
        TestModel(),
        deps_type=LayerDeps,
        capabilities=[layers_capability(layers, get_layers=get_layers)],
    )
    with pytest.raises(UserError, match="example_echo"):
        await agent.run(
            "hello", deps=LayerDeps(layers=load_layers({"one": {}, "two": {}}, {}, layers))
        )


@pytest.mark.asyncio
async def test_tool_definitions_preserve_native_flags_and_declared_ownership():
    def model(messages, info):
        (definition,) = info.function_tools
        assert definition.name == "approved_echo" and definition.kind == "unapproved"
        assert definition.metadata == {"category": "example"}
        assert definition.strict is True and definition.sequential is True
        assert definition.capability_id == "declared_owner"
        return ModelResponse(parts=[TextPart("checked")])

    layers = make_layers({"approval": tool_definitions.make}, get_layers)
    agent = Agent(
        FunctionModel(model),
        deps_type=LayerDeps,
        output_type=str | DeferredToolRequests,
        capabilities=[layers_capability(layers, get_layers=get_layers)],
    )
    assert (
        await agent.run("hello", deps=LayerDeps(layers=load_layers({"approval": {}}, {}, layers)))
    ).output == "checked"


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_deferred_loading_preserves_native_capability_and_toolset_instructions(nested):
    def make(
        layer_id: str, access: LayerAccess[LayerDeps]
    ) -> Layer[LayerDeps, echo.Config, echo.State]:
        layer = deferred.make("lazy", access)
        return replace(
            layer, capability=CombinedCapability(id=layer_id, capabilities=[layer.capability])
        )

    name = "container" if nested else "lazy"
    layers = make_layers({name: make if nested else deferred.make}, get_layers)
    agent = Agent(
        FunctionModel(deferred.lazy_model),
        deps_type=LayerDeps,
        capabilities=[
            layers_capability(
                layers,
                get_layers=get_layers,
                wrap_toolset=(lambda _, toolset: WrapperToolset(toolset)) if nested else None,
            )
        ],
    )
    result = await agent.run(
        "load lazy", deps=LayerDeps(layers=load_layers({name: {}}, {}, layers))
    )
    assert json.loads(result.output) == {
        "instructions": {"instructions": "CAPABILITY_RULE\n\nTOOLSET_RULE"},
        "tool_result": "lazy-result",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_id", [None, ""])
async def test_nested_tool_owners_require_nonempty_declared_ids(capability_id):
    def make(
        layer_id: str, access: LayerAccess[LayerDeps]
    ) -> Layer[LayerDeps, echo.Config, echo.State]:
        def tool() -> str:
            return "result"

        return Layer(
            config_type=echo.Config,
            state_type=echo.State,
            capability=CombinedCapability(
                id=layer_id, capabilities=[Capability(id=capability_id, tools=[tool])]
            ),
        )

    layers = make_layers({"invalid": make}, get_layers)
    agent = Agent(
        TestModel(),
        deps_type=LayerDeps,
        capabilities=[layers_capability(layers, get_layers=get_layers)],
    )
    with pytest.raises(UserError, match="nonempty stable id"):
        await agent.run(
            "call tool", deps=LayerDeps(layers=load_layers({"invalid": {}}, {}, layers))
        )

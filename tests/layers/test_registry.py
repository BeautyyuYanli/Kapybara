"""Fixed module exports and explicit JSON state through actual native SDK runs."""

import asyncio
import json
from types import ModuleType

import pytest
from pydantic import BaseModel, RootModel, ValidationError, model_validator
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import Capability as NativeCapability
from pydantic_ai.capabilities import CombinedCapability
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.toolsets import FunctionToolset, WrapperToolset

from kapy.layers import LayerDeps, create_layers, layers_capability, load_layers, snapshot_layers
from kapy.layers.examples import echo, new_layer
from kapy.layers.types import JsonObject
from layers import deferred, tool_definitions


def module(config=echo.Config, state=echo.State, **components):
    result = ModuleType("test_layer")
    result.__dict__.update({"Config": config, "State": state, **components})
    return result


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
    layers = load_layers(configs, states, {"echo": echo})
    deps = LayerDeps(layers=layers)
    assert LayerDeps.model_validate_json(deps.model_dump_json()) == deps
    configs["echo"]["prefix"] = "changed"
    states["echo"]["calls"] = 10
    validated = echo.State.model_validate(deps.layers["echo"]["state"])
    validated.calls += 1
    assert snapshot_layers(deps, {"echo": echo}) == {"echo": {"calls": 3}}
    deps.layers["echo"]["state"] = validated.model_dump(mode="json")
    snapshot = snapshot_layers(deps, {"echo": echo})
    assert snapshot == {"echo": {"calls": 4}}
    snapshot["echo"]["calls"] = 20
    assert deps.layers["echo"]["state"] == {"calls": 4}
    assert deps.layers["echo"]["config"] == {"prefix": "A:", "uppercase": False}
    assert load_layers({"echo": {}}, {}, {"echo": echo})["echo"]["state"] == {"calls": 0}


@pytest.mark.parametrize(
    "invalid",
    [
        module(Capability=echo.Capability, Toolset=FunctionToolset),
        module(),
        module(Capability=str),
        module(config=dict, Capability=echo.Capability),
        module(state=dict, Capability=echo.Capability),
    ],
)
def test_module_exports_are_checked_before_construction(invalid):
    with pytest.raises(UserError):
        load_layers({"invalid": {}}, {}, {"invalid": invalid})


def test_unknown_names_invalid_json_and_nonobject_models_fail():
    with pytest.raises(UserError, match="Unknown registered layer"):
        load_layers({"unknown": {}}, {}, {"echo": echo})
    with pytest.raises(UserError, match="unconfigured"):
        load_layers({}, {"echo": {}}, {"echo": echo})
    invalid: list[tuple[dict[str, JsonObject], dict[str, JsonObject]]] = [
        ({"echo": {"unknown": True}}, {}),
        ({"echo": {}}, {"echo": {"calls": []}}),
    ]
    for configs, states in invalid:
        with pytest.raises(ValidationError):
            load_layers(configs, states, {"echo": echo})
    nonobject = module(config=RootModel[None], Capability=echo.Capability)
    with pytest.raises(UserError, match="JSON objects"):
        load_layers({"null": None}, {}, {"null": nonobject})  # pyrefly: ignore[bad-assignment]


def test_load_copies_inputs_before_custom_validators():
    class Config(BaseModel):
        items: list[str]

        @model_validator(mode="before")
        @classmethod
        def append(cls, value):
            value["items"].append("validated")
            return value

    configs: dict[str, JsonObject] = {"copy": {"items": ["input"]}}
    loaded = load_layers(configs, {}, {"copy": module(config=Config, Capability=echo.Capability)})
    assert configs == {"copy": {"items": ["input"]}}
    assert loaded["copy"]["config"] == {"items": ["input", "validated"]}


@pytest.mark.asyncio
async def test_same_agent_reconfigures_disables_and_keeps_json_state_per_run():
    registry = {"echo": echo}
    agent = Agent(
        FunctionModel(echo_model), deps_type=LayerDeps, capabilities=[layers_capability(registry)]
    )
    inputs = [
        LayerDeps(layers=load_layers({"echo": {"prefix": "A:"}}, {}, registry)),
        LayerDeps(layers=load_layers({"echo": {"prefix": "B:", "uppercase": True}}, {}, registry)),
        LayerDeps(),
    ]
    results = await asyncio.gather(*(agent.run("hello", deps=deps) for deps in inputs))
    assert [result.output for result in results] == ["A:hello", "B:HELLO", "disabled"]
    assert snapshot_layers(inputs[0], registry) == {"echo": {"calls": 1}}
    assert snapshot_layers(inputs[1], registry) == {"echo": {"calls": 1}}
    assert (await agent.run("hello", deps=inputs[0])).output == "A:hello"
    assert snapshot_layers(inputs[0], registry) == {"echo": {"calls": 2}}
    first = create_layers(inputs[0], registry)
    second = create_layers(inputs[0], registry)
    assert first["echo"] is not second["echo"]
    assert first["echo"].id == "echo"


@pytest.mark.asyncio
async def test_entrance_captures_module_registry_and_rejects_bad_run_input():
    registry: dict[str, ModuleType] = {"echo": echo}
    agent = Agent(TestModel(), deps_type=LayerDeps, capabilities=[layers_capability(registry)])
    registry["new"] = new_layer
    for name, config in (("new", {"instructions": "new"}), ("echo", {"extra": True})):
        with pytest.raises(UserError):
            await agent.run("hello", deps=LayerDeps(layers={name: {"config": config, "state": {}}}))
    updated = Agent(TestModel(), deps_type=LayerDeps, capabilities=[layers_capability(registry)])
    assert (
        await updated.run(
            "hello",
            deps=LayerDeps(layers=load_layers({"new": {"instructions": "new"}}, {}, registry)),
        )
    ).output == "success (no tool calls)"


@pytest.mark.asyncio
async def test_toolset_export_and_tool_name_conflicts_use_native_sdk():
    class Toolset(FunctionToolset[LayerDeps]):
        def __init__(self, name: str):
            self.name = name

            def tool(ctx: RunContext[LayerDeps], text: str) -> str:
                return echo.Config.model_validate(ctx.deps.layers[name]["config"]).prefix + text

            super().__init__(id=name, tools=[tool])

    registry = {"tools": module(Toolset=Toolset)}
    agent = Agent(TestModel(), deps_type=LayerDeps, capabilities=[layers_capability(registry)])
    assert json.loads(
        (
            await agent.run(
                "hello",
                deps=LayerDeps(layers=load_layers({"tools": {"prefix": "T:"}}, {}, registry)),
            )
        ).output
    ) == {"tool": "T:a"}
    conflict = {"one": echo, "two": echo}
    agent = Agent(TestModel(), deps_type=LayerDeps, capabilities=[layers_capability(conflict)])
    with pytest.raises(UserError, match="example_echo"):
        await agent.run(
            "hello", deps=LayerDeps(layers=load_layers({"one": {}, "two": {}}, {}, conflict))
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

    registry = {"approval": tool_definitions}
    agent = Agent(
        FunctionModel(model),
        deps_type=LayerDeps,
        output_type=str | DeferredToolRequests,
        capabilities=[layers_capability(registry)],
    )
    assert (
        await agent.run("hello", deps=LayerDeps(layers=load_layers({"approval": {}}, {}, registry)))
    ).output == "checked"


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True])
async def test_deferred_loading_preserves_native_capability_and_toolset_instructions(nested):
    class Capability(CombinedCapability[LayerDeps]):
        def __init__(self, name: str):
            self.name = name
            super().__init__(id=name, capabilities=[deferred.Capability("lazy")])

    name = "container" if nested else "lazy"
    registry = {name: module(Capability=Capability) if nested else deferred}
    agent = Agent(
        FunctionModel(deferred.lazy_model),
        deps_type=LayerDeps,
        capabilities=[
            layers_capability(
                registry,
                wrap_toolset=(lambda _, toolset: WrapperToolset(toolset)) if nested else None,
            )
        ],
    )
    result = await agent.run(
        "load lazy", deps=LayerDeps(layers=load_layers({name: {}}, {}, registry))
    )
    assert json.loads(result.output) == {
        "instructions": {"instructions": "CAPABILITY_RULE\n\nTOOLSET_RULE"},
        "tool_result": "lazy-result",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_id", [None, ""])
async def test_nested_tool_owners_require_nonempty_declared_ids(capability_id):
    class Capability(CombinedCapability[LayerDeps]):
        def __init__(self, name: str):
            self.name = name

            def tool() -> str:
                return "result"

            super().__init__(
                id=name,
                capabilities=[NativeCapability(id=capability_id, tools=[tool])],
            )

    registry = {"invalid": module(Capability=Capability)}
    agent = Agent(TestModel(), deps_type=LayerDeps, capabilities=[layers_capability(registry)])
    with pytest.raises(UserError, match="nonempty stable id"):
        await agent.run(
            "call tool", deps=LayerDeps(layers=load_layers({"invalid": {}}, {}, registry))
        )

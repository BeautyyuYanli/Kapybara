"""The host opens local handles while the SDK consumes them through deps."""

import asyncio
from collections.abc import Mapping
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any

import anyio
import pytest
from anyio.abc import TaskGroup
from anyio.lowlevel import checkpoint
from pydantic import ValidationError
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import Capability
from pydantic_ai.exceptions import UserError
from pydantic_ai.models.test import TestModel

from pydantic_ai_layer_protocol import (
    JsonObject,
    Layer,
    LayerAccess,
    LayerData,
    LayerDeps,
    LayerRegistry,
    dump_object,
)
from pydantic_ai_layer_protocol.examples.echo import Config, State


@dataclass(frozen=True)
class Handle:
    conversation_id: str
    label: str


def get_layers(deps: LayerDeps) -> dict[str, LayerData]:
    return deps.layers


def get_resources(deps: LayerDeps) -> Mapping[str, Any]:
    return deps.resources


@pytest.fixture
def resource_layers():
    events: list[tuple[str, Handle]] = []
    waiting = asyncio.Event()
    accesses: dict[str, LayerAccess[LayerDeps]] = {}

    async def enter(conversation_id: str, config: Config) -> Handle:
        handle = Handle(conversation_id, config.prefix)
        events.append(("enter", handle))
        if config.prefix == "fail":
            raise RuntimeError("enter failed")
        if config.prefix == "wait":
            waiting.set()
            await asyncio.Future()
        return handle

    async def exit(resource: Handle) -> None:
        await checkpoint()
        events.append(("exit", resource))

    def make(
        layer_id: str, access: LayerAccess[LayerDeps]
    ) -> Layer[LayerDeps, Config, State, Handle]:
        accesses[layer_id] = access

        def use(ctx: RunContext[LayerDeps]) -> str:
            resource: Handle = access.resource(ctx.deps)
            entry = access.data(ctx.deps)
            state = State.model_validate(entry["state"])
            state.calls += 1
            entry["state"] = dump_object(state)
            return resource.label

        return Layer(
            config_type=Config,
            state_type=State,
            capability=Capability(
                id=layer_id,
                tools=[] if layer_id == "plain" else [Tool(use, name=f"use_{layer_id}")],
            ),
            enter_conversation=enter if layer_id != "plain" else None,
            exit_conversation=exit if layer_id in {"one", "two"} else None,
        )

    return make, events, waiting, accesses


def test_resource_hook_assembly_requires_enter_and_resource_adapter(resource_layers):
    make, *_ = resource_layers
    with pytest.raises(UserError, match="requires get_resources"):
        LayerRegistry({"one": make}, get_layers=get_layers)

    async def exit(resource: Handle) -> None:
        pytest.fail("A registry with an invalid hook must not open")

    def orphan(
        layer_id: str, access: LayerAccess[LayerDeps]
    ) -> Layer[LayerDeps, Config, State, Handle]:
        return Layer(
            config_type=Config,
            state_type=State,
            capability=Capability(id=layer_id),
            exit_conversation=exit,
        )

    with pytest.raises(UserError, match="requires enter_conversation"):
        LayerRegistry({"orphan": orphan}, get_layers=get_layers, get_resources=get_resources)


@pytest.mark.asyncio
async def test_open_resources_are_reused_by_runs_and_excluded_from_json(resource_layers):
    make, events, _, accesses = resource_layers
    registry = LayerRegistry(
        {name: make for name in ("one", "two", "memory", "plain")},
        get_layers=get_layers,
        get_resources=get_resources,
    )
    configs: dict[str, JsonObject] = {
        "two": {"prefix": "second"},
        "plain": {},
        "memory": {"prefix": "memory"},
        "one": {"prefix": "first"},
    }
    deps = LayerDeps(layers=registry.load(configs, {}))
    assert registry.snapshot(deps) == {name: {"calls": 0} for name in configs}
    assert not events
    model = TestModel()
    agent = Agent(model, deps_type=LayerDeps, capabilities=[registry])
    with pytest.raises(UserError, match="'two' has no open resource"):
        await agent.run("use", deps=deps)
    assert model.last_model_request_parameters is None

    async with registry.open_conversation("conversation", configs) as resources:
        deps.resources = resources
        assert list(resources) == ["two", "memory", "one"]
        for name, resource in resources.items():
            assert resource.conversation_id == "conversation"
            assert accesses[name].resource(deps) is resource
        with pytest.raises(UserError, match="'plain' has no open resource"):
            accesses["plain"].resource(deps)
        for _ in range(2):
            result = await agent.run("use", deps=deps, conversation_id="conversation")
            for resource in resources.values():
                assert resource.label in result.output
        assert registry.snapshot(deps) == {
            "two": {"calls": 2},
            "plain": {"calls": 0},
            "memory": {"calls": 2},
            "one": {"calls": 2},
        }
        assert set(deps.model_dump()) == {"layers"}
        assert LayerDeps.model_validate_json(deps.model_dump_json()).layers == deps.layers
        assert events == [("enter", resource) for resource in resources.values()]
    assert [kind for kind, _ in events] == ["enter", "enter", "enter", "exit", "exit"]
    assert events[-2][1] is resources["one"] and events[-1][1] is resources["two"]


@pytest.mark.asyncio
async def test_all_configs_and_identity_are_validated_before_opening_handles(resource_layers):
    make, events, *_ = resource_layers
    registry = LayerRegistry(
        {"one": make, "two": make}, get_layers=get_layers, get_resources=get_resources
    )
    with pytest.raises(ValidationError):
        async with registry.open_conversation("valid", {"one": {}, "two": {"uppercase": []}}):
            pytest.fail("Invalid Config entered the context")
    with pytest.raises(UserError, match="Unknown registered layer"):
        async with registry.open_conversation("valid", {"one": {}, "missing": {}}):
            pytest.fail("Unknown layer entered the context")
    with pytest.raises(UserError, match="nonempty conversation_id"):
        async with registry.open_conversation("", {"one": {}}):
            pytest.fail("Empty identity entered the context")
    assert not events


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_partial_entry_closes_only_successful_handles(resource_layers, cancel):
    make, events, waiting, *_ = resource_layers
    registry = LayerRegistry(
        {"one": make, "two": make}, get_layers=get_layers, get_resources=get_resources
    )

    async def open_partial() -> None:
        async with registry.open_conversation(
            "conversation",
            {"one": {"prefix": "first"}, "two": {"prefix": "wait" if cancel else "fail"}},
        ):
            pytest.fail("Partial entry reached the body")

    if cancel:
        task = asyncio.create_task(open_partial())
        await asyncio.wait_for(waiting.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(RuntimeError, match="enter failed"):
            await open_partial()
    assert [kind for kind, _ in events] == ["enter", "enter", "exit"]
    assert events[-1][1] is events[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("cancel_at", "expected"),
    [
        (None, ["two", "one"]),
        ("enter", ["one", "cancelled"]),
        ("exit", ["two", "one", "cancelled"]),
    ],
    ids=["normal", "cancel-enter", "cancel-exit"],
)
async def test_resource_hooks_preserve_task_and_context(cancel_at: str | None, expected: list[str]):
    current_layer = ContextVar("current_layer", default="caller")
    waiting, release_exit = anyio.Event(), anyio.Event()
    events: list[str] = []
    task_id: int | None = None

    def make(
        layer_id: str, access: LayerAccess[LayerDeps]
    ) -> Layer[LayerDeps, Config, State, tuple[TaskGroup, Token[str]]]:
        async def enter(conversation_id: str, config: Config) -> tuple[TaskGroup, Token[str]]:
            assert anyio.get_current_task().id == task_id
            assert current_layer.get() == ("caller" if layer_id == "one" else "one")
            if cancel_at == "enter" and layer_id == "two":
                waiting.set()
                await anyio.sleep_forever()
            group = anyio.create_task_group()
            await group.__aenter__()
            return group, current_layer.set(layer_id)

        async def exit(resource: tuple[TaskGroup, Token[str]]) -> None:
            assert anyio.get_current_task().id == task_id
            assert current_layer.get() == layer_id
            if cancel_at == "exit" and layer_id == "two":
                waiting.set()
                await release_exit.wait()
            await checkpoint()
            group, token = resource
            await group.__aexit__(None, None, None)
            current_layer.reset(token)
            events.append(layer_id)

        return Layer(
            config_type=Config,
            state_type=State,
            capability=Capability(id=layer_id),
            enter_conversation=enter,
            exit_conversation=exit,
        )

    registry = LayerRegistry(
        {"one": make, "two": make}, get_layers=get_layers, get_resources=get_resources
    )

    async def use_resources() -> None:
        nonlocal task_id
        task_id = anyio.get_current_task().id
        try:
            async with registry.open_conversation("conversation", {"one": {}, "two": {}}):
                assert cancel_at != "enter"
                assert anyio.get_current_task().id == task_id
                assert current_layer.get() == "two"
        except anyio.get_cancelled_exc_class():
            events.append("cancelled")
            raise
        finally:
            assert current_layer.get() == "caller"

    with anyio.fail_after(5):
        async with anyio.create_task_group() as group:
            group.start_soon(use_resources)
            if cancel_at is not None:
                try:
                    await waiting.wait()
                    group.cancel_scope.cancel()
                    with anyio.CancelScope(shield=True):
                        # Let cancellation reach the worker before releasing the exit hook.
                        await checkpoint()
                finally:
                    release_exit.set()

    assert events == expected

"""Actual Agent → MCPToolset → stdio gateway → upstream lifecycle and isolation."""

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from kapy.layers.mcps import Config, make, make_toolset
from kapy.layers.registry import layer_registry
from pydantic_ai_layer_protocol import (
    JsonObject,
    LayerData,
    LayerDeps,
    LayerRegistry,
    dump_object,
)


def get_layers(deps: LayerDeps) -> dict[str, LayerData]:
    return deps.layers


LAYERS_TEST = LayerRegistry({"mcps": make}, get_layers=get_layers)


def config(label: str, marker: Path, *, namespace: str = "up") -> JsonObject:
    return {
        "servers": {
            namespace: {
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(Path(__file__).with_name("mcp_upstream.py"))],
                "env": {"LABEL": label, "PID_FILE": str(marker)},
                "credential_env": ["API_KEY"],
            }
        },
        "credentials": {namespace: {"env": {"API_KEY": f"{label}-secret"}}},
    }


def model(messages, info):
    returns = [
        part for message in messages for part in message.parts if isinstance(part, ToolReturnPart)
    ]
    if returns:
        return ModelResponse(parts=[TextPart(str(returns[-1].content))])
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name) for tool in info.function_tools)
    tools = [tool for tool in info.function_tools if "_echo_" in tool.name]
    if not tools:
        return ModelResponse(parts=[TextPart("disabled")])
    assert len(tools) == 1 and tools[0].capability_id == "mcps"
    return ModelResponse(parts=[ToolCallPart(tools[0].name, {"text": "hello"})])


def assert_processes_closed(marker: Path) -> None:
    entries = [json.loads(line) for line in marker.read_text().splitlines()]
    assert entries
    for pid in {entry[field] for entry in entries for field in ("pid", "parent")}:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_registered_config_round_trip_retains_credentials_and_empty_state(tmp_path):
    assert "mcps" in layer_registry.definitions and layer_registry.definitions["mcps"].id == "mcps"
    cfg = Config.model_validate(config("alice", tmp_path / "pids.jsonl"))
    plain = dump_object(cfg)
    restored = Config.model_validate(plain)
    assert restored.credentials["up"].env["API_KEY"].get_secret_value() == "alice-secret"
    assert "alice-secret" not in repr(cfg)
    assert Config.model_validate_json(cfg.model_dump_json()) == cfg
    loaded = LAYERS_TEST.load({"mcps": plain}, {})
    assert loaded["mcps"]["state"] == {}
    assert isinstance(make_toolset(cfg).mcp, MCPToolset)
    assert not (tmp_path / "pids.jsonl").exists()


@pytest.mark.asyncio
async def test_same_agent_concurrently_uses_distinct_inline_gateways_and_disabled_selection(
    tmp_path,
):
    agent = Agent(
        FunctionModel(model),
        deps_type=LayerDeps,
        capabilities=[LAYERS_TEST],
    )
    markers = [tmp_path / f"{label}.jsonl" for label in ("alice", "bob")]
    inputs = [
        LayerDeps(
            layers=LAYERS_TEST.load(
                {"mcps": config(label, marker, namespace=label)},
                {},
            )
        )
        for label, marker in zip(("alice", "bob"), markers, strict=True)
    ]
    results = await asyncio.gather(
        *(agent.run("echo", deps=deps) for deps in inputs + [LayerDeps()])
    )
    assert [result.output for result in results] == [
        "alice:hello:alice-secret",
        "bob:hello:bob-secret",
        "disabled",
    ]
    assert all(LAYERS_TEST.snapshot(deps) == {"mcps": {}} for deps in inputs)
    for marker in markers:
        assert_processes_closed(marker)
    parent_sets = [
        {json.loads(line)["parent"] for line in marker.read_text().splitlines()}
        for marker in markers
    ]
    assert parent_sets[0].isdisjoint(parent_sets[1])
    assert (await agent.run("again", deps=inputs[0])).output == "alice:hello:alice-secret"
    assert_processes_closed(markers[0])


@pytest.mark.asyncio
async def test_model_aliases_keep_colliding_stems_distinct_bound_length_and_route_original_names(
    tmp_path,
):
    marker = tmp_path / "aliases.jsonl"
    discovered = []

    def alias_model(messages, info):
        returned = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if returned:
            assert {part.content for part in returned} == {"a.b", "a_b", "x" * 64}
            return ModelResponse(parts=[TextPart("aliases routed")])
        tools = [
            tool for tool in info.function_tools if (tool.description or "").startswith("alias:")
        ]
        assert len(tools) == 3
        assert len({tool.name for tool in tools}) == 3
        assert all(re.fullmatch(r"[A-Za-z0-9_-]{1,64}", tool.name) for tool in tools)
        discovered.append([tool.name for tool in tools])
        return ModelResponse(parts=[ToolCallPart(tool.name, {}) for tool in tools])

    agent = Agent(
        FunctionModel(alias_model),
        deps_type=LayerDeps,
        capabilities=[LAYERS_TEST],
    )
    deps = LayerDeps(layers=LAYERS_TEST.load({"mcps": config("aliases", marker)}, {}))
    for _ in range(2):
        assert (await agent.run("aliases", deps=deps)).output == "aliases routed"
        assert_processes_closed(marker)
    assert discovered[0] == discovered[1]


@pytest.mark.asyncio
async def test_direct_mcp_toolset_closes_gateway_and_upstream_after_tool_error(tmp_path):
    marker = tmp_path / "error.jsonl"
    toolset = make_toolset(Config.model_validate(config("error", marker)))
    async with toolset:
        assert (
            await toolset.mcp.direct_call_tool("up.echo", {"text": "hello"})
            == "error:hello:error-secret"
        )
        with pytest.raises(ModelRetry):
            await toolset.mcp.direct_call_tool("up.fail", {})
    assert_processes_closed(marker)


@pytest.mark.asyncio
async def test_cancelled_run_releases_gateway_and_inflight_upstream(tmp_path):
    marker = tmp_path / "cancel.jsonl"

    def blocked_model(messages, info):
        tool = next(tool for tool in info.function_tools if tool.name.startswith("up_block_"))
        return ModelResponse(parts=[ToolCallPart(tool.name, {})])

    agent = Agent(
        FunctionModel(blocked_model),
        deps_type=LayerDeps,
        capabilities=[LAYERS_TEST],
    )
    deps = LayerDeps(layers=LAYERS_TEST.load({"mcps": config("cancel", marker)}, {}))
    task = asyncio.create_task(agent.run("block", deps=deps))
    try:
        async with asyncio.timeout(20):
            while not marker.exists() or '"block"' not in marker.read_text():
                if task.done():
                    task.result()
                    pytest.fail("Agent finished before the blocking tool started")
                await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert_processes_closed(marker)


@pytest.mark.parametrize(
    "value",
    [
        {"credentials": {"unknown": {"bearer": "secret"}}},
        {
            "servers": {"api": {"transport": "http", "url": "http://localhost/mcp"}},
            "credentials": {"api": {"custom": {"secret": "unsupported adapter"}}},
        },
        {"init_timeout": 0},
    ],
)
def test_invalid_layer_config_is_rejected_before_any_process(value):
    with pytest.raises(ValidationError):
        LAYERS_TEST.load({"mcps": value}, {})

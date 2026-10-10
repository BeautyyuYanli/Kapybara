"""Compare layer ownership with the installed SDK and require review of copied code.

The patch copies CapabilityOwnedToolset.get_tools because the SDK has no owner-ID
override hook. An SDK upgrade must not silently change that method or the fields
we reconstruct. The AST guard ignores formatting/comments; update it only after
reviewing the patch against the new upstream method, not merely to unblock tests.
"""

import ast
import hashlib
import inspect
import json
import textwrap
from dataclasses import fields, replace

import pytest
from pydantic_ai import Agent, RunContext, Tool
from pydantic_ai.capabilities import Capability, CombinedCapability, WrapperCapability
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import ToolDefinition
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset, PreparedToolset, ToolsetTool
from pydantic_ai.toolsets._capability_owned import CapabilityOwnedToolset
from pydantic_ai.usage import RunUsage

from kapy.layers import LayerDeps, layers_capability, load_layers
from kapy.layers.composition import _LayerCapabilities
from layers import deferred


def test_copied_sdk_ownership_logic_requires_review_on_change():
    source = textwrap.dedent(inspect.getsource(CapabilityOwnedToolset.get_tools))
    tree = ast.dump(ast.parse(source), include_attributes=False)
    digest = hashlib.sha256(tree.encode()).hexdigest()
    # Reviewed SDK 2.54.0: only the owner-ID lookup is replaced by the layer patch.
    assert digest == "0635efd6a98d28b8f2d2f7eedd28c956a3b685af39c972490342e18bed7672c7", (
        "Upstream CapabilityOwnedToolset.get_tools changed; review the layer ownership "
        "patch and differential tests before updating this fingerprint."
    )
    assert {field.name for field in fields(CapabilityOwnedToolset)} == {"wrapped", "capability"}, (
        "Upstream ownership fields changed; review which state the layer patch must preserve."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("capability_deferred", [None, False, True])
@pytest.mark.parametrize("tool_deferred", [False, True])
@pytest.mark.parametrize("declared_owner", [None, "declared"])
@pytest.mark.parametrize("nested", [False, True])
async def test_patched_composition_matches_installed_sdk(
    capability_deferred, tool_deferred, declared_owner, nested
):
    def echo(text: str) -> str:
        return text

    def validate(ctx: RunContext[LayerDeps], text: str) -> None:
        assert text

    def prepare(ctx: RunContext[LayerDeps], definitions: list[ToolDefinition]):
        return [replace(definition, capability_id=declared_owner) for definition in definitions]

    toolset = PreparedToolset[LayerDeps](
        wrapped=FunctionToolset[LayerDeps](
            id="tools",
            instructions="TOOLSET_RULE",
            tools=[
                Tool(
                    echo,
                    max_retries=3,
                    args_validator=validate,
                    strict=True,
                    sequential=True,
                    requires_approval=True,
                    metadata={"category": "contract"},
                    timeout=2,
                    defer_loading=tool_deferred,
                    include_return_schema=True,
                )
            ],
        ),
        prepare_func=prepare,
    )
    owner = Capability[LayerDeps](id="owner", defer_loading=capability_deferred, toolsets=[toolset])
    # Keep an inner native Combined behind a wrapper so flattening cannot erase it.
    root = (
        WrapperCapability[LayerDeps](
            CombinedCapability[LayerDeps](capabilities=[owner]), id="outer"
        )
        if nested
        else owner
    )
    ctx = RunContext(
        deps=LayerDeps(),
        model=TestModel(),
        usage=RunUsage(),
        prompt="discover",
        capabilities={"owner": owner, "outer": root},
    )
    native = CombinedCapability[LayerDeps](capabilities=[root]).get_toolset()
    patched = _LayerCapabilities[LayerDeps](capabilities=[root]).get_toolset()
    assert isinstance(native, AbstractToolset) and isinstance(patched, AbstractToolset)
    expected, actual = await native.get_tools(ctx), await patched.get_tools(ctx)
    assert actual.keys() == expected.keys()
    for name in expected:
        # Compare the whole public definition and dispatch payload, including new
        # SDK fields, rather than a hand-picked list of currently known flags.
        assert {field.name: getattr(actual[name], field.name) for field in fields(ToolsetTool)} == {
            field.name: getattr(expected[name], field.name) for field in fields(ToolsetTool)
        }
        args = {"text": "result"}
        assert await patched.call_tool(name, args, ctx, actual[name]) == await native.call_tool(
            name, args, ctx, expected[name]
        )
    assert await patched.get_instructions(ctx) == await native.get_instructions(ctx)


@pytest.mark.asyncio
async def test_deferred_loading_matches_installed_sdk():
    registry = {"lazy": deferred}
    native = Agent(
        FunctionModel(deferred.lazy_model),
        deps_type=LayerDeps,
        capabilities=[deferred.Capability("lazy")],
    )
    patched = Agent(
        FunctionModel(deferred.lazy_model),
        deps_type=LayerDeps,
        capabilities=[layers_capability(registry)],
    )
    deps = LayerDeps(layers=load_layers({"lazy": {}}, {}, registry))
    expected = await native.run("load lazy", deps=deps)
    actual = await patched.run("load lazy", deps=deps)
    assert json.loads(actual.output) == json.loads(expected.output)

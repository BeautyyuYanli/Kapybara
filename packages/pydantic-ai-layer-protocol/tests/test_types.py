"""Real Pyrefly probes keep model and deps associations at the public boundary."""

import subprocess
from pathlib import Path

import pytest

SOURCE = """from uuid import UUID
from typing import assert_type
from pydantic import BaseModel
from pydantic_ai.capabilities import Capability
from pydantic_ai_layer_protocol import Layer, LayerAccess, LayerData, LayerDeps, LayerRegistry

class Config(BaseModel):
    prefix: str

class State(BaseModel):
    calls: int = 0

class Resource:
    pass

async def enter(conversation_id: str, config: Config) -> Resource:
    return Resource()

async def exit(resource: Resource) -> None:
    pass

layer = Layer[LayerDeps, Config, State, Resource](
    config_type=Config,
    state_type=State,
    capability=Capability[LayerDeps](id="typed"),
    enter_conversation=enter,
    exit_conversation=exit,
)

def make(
    layer_id: str, access: LayerAccess[LayerDeps]
) -> Layer[LayerDeps, Config, State, Resource]:
    assert_type(access.data(LayerDeps()), LayerData)
    return layer

def get_layers(deps: LayerDeps) -> dict[str, LayerData]:
    return deps.layers

registry = LayerRegistry(
    {"typed": make}, get_layers=get_layers, get_resources=lambda deps: deps.resources
)
assert_type(registry, LayerRegistry[LayerDeps])
"""


@pytest.mark.parametrize(
    "old,new,diagnostic",
    [
        ("", "", None),
        ("resource: Resource", "resource: State", "exit_conversation"),
        ("resource: Resource", "resource: Config", "exit_conversation"),
        (
            "async def enter(conversation_id: str, config: Config)",
            "async def enter(conversation_id: str, config: State)",
            "enter_conversation",
        ),
        ("conversation_id: str", "conversation_id: UUID", "enter_conversation"),
        (
            "-> Resource:\n    return Resource()",
            "-> Config:\n    return config",
            "enter_conversation",
        ),
        (
            "-> None:\n    pass",
            "-> Resource:\n    return Resource()",
            "exit_conversation",
        ),
        ("Capability[LayerDeps]", "Capability[int]", "capability"),
        ('    capability=Capability[LayerDeps](id="typed"),\n', "", "capability"),
    ],
    ids=[
        "resource_exit",
        "state_is_not_resource",
        "config_is_not_resource",
        "wrong_config",
        "uuid_identity",
        "enter_return",
        "exit_return",
        "wrong_deps",
        "missing_capability",
    ],
)
def test_layer_hook_types_are_checked_with_project_configuration(tmp_path, old, new, diagnostic):
    probe = tmp_path / "layer_types.py"
    probe.write_text(SOURCE.replace(old, new) if old else SOURCE)
    config = Path(__file__).resolve().parents[1] / "pyproject.toml"
    result = subprocess.run(
        ["pyrefly", "check", "--config", str(config), str(probe)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    output = result.stdout + result.stderr
    if diagnostic is None:
        assert result.returncode == 0, output
    else:
        assert result.returncode == 1 and diagnostic in output, output

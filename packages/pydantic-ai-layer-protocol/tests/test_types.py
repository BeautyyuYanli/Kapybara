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
    resource: str

async def enter(scope_id: UUID, config: Config) -> State:
    return State(resource=config.prefix)

async def exit(scope_id: UUID, config: Config, state: State | None) -> None:
    pass

layer = Layer[LayerDeps, Config, State](
    config_type=Config,
    state_type=State,
    capability=Capability[LayerDeps](id="typed"),
    enter_scope=enter,
    exit_scope=exit,
)

def make(layer_id: str, access: LayerAccess[LayerDeps]) -> Layer[LayerDeps, Config, State]:
    return layer

def get_layers(deps: LayerDeps) -> dict[str, LayerData]:
    return deps.layers

registry = LayerRegistry({"typed": make}, get_layers=get_layers)
assert_type(registry, LayerRegistry[LayerDeps])
"""


@pytest.mark.parametrize(
    "old,new,diagnostic",
    [
        ("", "", None),
        ("state: State | None", "state: State", "exit_scope"),
        ("state: State | None", "state: Config | None", "exit_scope"),
        (
            "async def enter(scope_id: UUID, config: Config)",
            "async def enter(scope_id: UUID, config: State)",
            "enter_scope",
        ),
        (
            "-> State:\n    return State(resource=config.prefix)",
            "-> Config:\n    return config",
            "enter_scope",
        ),
        ("-> None:\n    pass", "-> State:\n    return State(resource=config.prefix)", "exit_scope"),
        ("Capability[LayerDeps]", "Capability[int]", "capability"),
        ('    capability=Capability[LayerDeps](id="typed"),\n', "", "capability"),
    ],
    ids=[
        "optional_exit",
        "required_state_exit",
        "wrong_state",
        "wrong_config",
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

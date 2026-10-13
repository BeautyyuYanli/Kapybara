"""Kapy's single layer registry shared by the Agent and session lifecycle.

Runner and SessionService share these definitions. Execution data lives only in
RunnerDeps; TYPE_CHECKING keeps static assembly independent of the runner/Agent.
Changing the installed set requires constructing a new registry.
"""

from typing import TYPE_CHECKING

from pydantic_ai_layer_protocol import LayerData, LayerRegistry

from .shell import layer as shell

if TYPE_CHECKING:
    from kapy.runner.types import RunnerDeps


def get_layers(deps: RunnerDeps) -> dict[str, LayerData]:
    """Return the writable mapping in the current runner payload."""
    return deps.layers


layer_registry = LayerRegistry({"shell": shell.make}, get_layers=get_layers)

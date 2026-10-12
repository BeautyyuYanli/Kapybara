"""Kapy's installed factories and one process-local static Layer configuration.

Runner and SessionService share these definitions. Execution data lives only in
RunnerDeps; TYPE_CHECKING keeps static assembly independent of the runner/Agent.
Changing the installed set requires assembling a new configuration.
"""

from typing import TYPE_CHECKING

from .mcps import layer as mcps
from .modules import make_layers
from .shell import layer as shell
from .types import LayerData, LayerFactory

if TYPE_CHECKING:
    from kapy.runner_duarable.types import RunnerDeps


def get_layers(deps: RunnerDeps) -> dict[str, LayerData]:
    """Return the writable mapping in the current runner payload."""
    return deps.layers


REGISTRY: dict[str, LayerFactory[RunnerDeps]] = {"shell": shell.make, "mcps": mcps.make}
LAYERS = make_layers(REGISTRY, get_layers)

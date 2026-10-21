"""Kapy's single layer registry shared by the Agent and session lifecycle.

Runner and SessionService share these definitions. Execution data lives only in
RunnerDeps; TYPE_CHECKING keeps static assembly independent of the runner/Agent.
Changing the installed set requires constructing a new registry.
"""

from typing import TYPE_CHECKING

from pydantic_ai_layer_protocol import LayerData, LayerRegistry

from .mcps import layer as mcps
from .shell import layer as shell

if TYPE_CHECKING:
    from kapy.runner.types import RunnerDeps


def get_layers(deps: RunnerDeps) -> dict[str, LayerData]:
    """Return the writable mapping in the current runner payload."""
    return deps.layers


# Business layer contract:
# Edit only working history already carrying metadata.seq. Preserve the last
# assigned seq value (message content may change), keep retained seq values valid
# and strictly increasing, and leave the unnumbered tail unchanged without assigning
# it seq values. Preserve valid message structure and tool call/result pairing,
# including pairs spanning the numbered/unnumbered boundary.
# Any capability method may be retried. Hooks need not be strictly idempotent:
# acceptable non-critical repeated side effects are allowed, but critical ones are not.
# Execute tools only for explicit model calls already saved in the snapshot at the
# end of the model response. Execution must match the saved call semantics (tool
# identity, call ID and business arguments); no implicit calls or silent changes
# to the recorded intent.
# Layer exit_scope hooks must use scope_id to find or clean upstream resources
# when supported. Otherwise, cleanup may rely on State, but the implementation
# must document that State may be stale or incomplete and leave orphaned resources.
layer_registry = LayerRegistry({"shell": shell.make, "mcps": mcps.make}, get_layers=get_layers)

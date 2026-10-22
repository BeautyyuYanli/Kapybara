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
# Resource-creating tools/hooks must mark upstream resources or their scope with
# conversation ownership when supported. Derive only the identity needed to locate
# this conversation's resources from conversation_id, layer_id and fixed Config;
# the model must not choose or override it. Tools/SDK hooks read ctx.conversation_id;
# the host passes the same string to independent enter_conversation/exit_conversation.
# exit_conversation must reconstruct the marker and reclaim all owned resources,
# including those absent from State. If upstream cannot query/clean by ownership,
# State-based cleanup is best effort and must document possible orphaned resources.
layer_registry = LayerRegistry({"shell": shell.make, "mcps": mcps.make}, get_layers=get_layers)

"""Process-local static Agent; each Activity supplies its own model and writable deps.

Factories perform no I/O. Per-run native SDK scopes own tool resources; only the
Activity constructs and closes its model and provider.
"""

from pydantic_ai import Agent
from pydantic_ai.capabilities import CapabilityOrdering, Hooks

from kapy.layers.registry import layer_registry

from .recording import MessageRecordCapability, Recorder
from .types import RunnerDeps

recorder = Recorder()
agent = Agent(
    name="runner",
    deps_type=RunnerDeps,
    capabilities=[
        MessageRecordCapability(recorder),
        layer_registry,
        Hooks(
            before_model_request=recorder.predict_response_seq,
            ordering=CapabilityOrdering(
                position="innermost",
                requires=[MessageRecordCapability],
            ),
        ),
    ],
)

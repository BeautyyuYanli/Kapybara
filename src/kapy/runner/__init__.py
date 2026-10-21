"""Standalone Temporal Activity runner; callers own Client and Worker lifecycle.

Register RunnerActivity.run on a Worker using Temporal's native Pydantic converter.
The static Agent and ordinary hooks execute entirely inside that Activity.
"""

from .activities import RunnerActivity
from .agent import agent
from .recording import Recorder, RunnerPersistenceCapability
from .types import ExecutionConfig, RunnerDeps, RunnerInput

__all__ = [
    "ExecutionConfig",
    "Recorder",
    "RunnerActivity",
    "RunnerDeps",
    "RunnerInput",
    "RunnerPersistenceCapability",
    "agent",
]

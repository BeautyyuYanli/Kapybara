"""Standalone Temporal runner; callers own Client and Worker lifecycle.

Use the SDK PydanticAIPlugin for payload/sandbox configuration and register
`agent` through AgentPlugin alongside RunnerWorkflow. This module does not
resolve database configuration, acquire leases, or install business plugins.
"""

from temporalio import workflow

# Model configuration helpers transitively import SQLModel table definitions;
# reuse those modules instead of reinitializing their types in each sandbox.
with workflow.unsafe.imports_passed_through():
    from .agent import agent
    from .types import DurableExecutionConfig, RunnerInput
from .workflow import RunnerWorkflow

__all__ = ["DurableExecutionConfig", "RunnerInput", "RunnerWorkflow", "agent"]

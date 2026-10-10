"""Sandboxed Workflow for the real resource-lifecycle integration test."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from kapy.layers import LayerDeps
    from layers.temporal_agent import agent, approval_agent, wrapped_agent


@workflow.defn
class ResourceWorkflow:
    @workflow.run
    async def run(self, deps: LayerDeps, wrapped: bool = False) -> str:
        selected = wrapped_agent if wrapped else agent
        return (await selected.run("hello", deps=deps)).output


@workflow.defn
class ApprovalWorkflow:
    @workflow.run
    async def run(self, deps: LayerDeps) -> str:
        output = (await approval_agent.run("inspect definition", deps=deps)).output
        assert isinstance(output, str)
        return output

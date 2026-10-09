"""Sandboxed Workflow for the real resource-lifecycle integration test."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from capability_plugins.temporal_agent import agent, approval_agent
    from kapy.capability_plugins import PluginDeps


@workflow.defn
class ResourceWorkflow:
    @workflow.run
    async def run(self, deps: PluginDeps) -> str:
        return (await agent.run("hello", deps=deps)).output


@workflow.defn
class ApprovalWorkflow:
    @workflow.run
    async def run(self, deps: PluginDeps) -> str:
        output = (await approval_agent.run("inspect definition", deps=deps)).output
        assert isinstance(output, str)
        return output

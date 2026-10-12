"""Sandboxed Workflow for the real resource-lifecycle integration test."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from layer_protocol_tests.temporal_agent import (
        agent,
        approval_agent,
        deferred_agent,
        wrapped_agent,
    )
    from pydantic_ai_layer_protocol import LayerDeps


@workflow.defn
class ResourceWorkflow:
    @workflow.run
    async def run(self, deps: LayerDeps, wrapped: bool = False) -> str:
        selected = wrapped_agent if wrapped else agent
        result = await selected.run("hello", deps=deps)
        if wrapped and "echo" in deps.layers:
            assert deps.layers["echo"]["state"] == {"calls": 1}
        return result.output


@workflow.defn
class ApprovalWorkflow:
    @workflow.run
    async def run(self, deps: LayerDeps) -> str:
        output = (await approval_agent.run("inspect definition", deps=deps)).output
        assert isinstance(output, str)
        return output


@workflow.defn
class DeferredWorkflow:
    @workflow.run
    async def run(self, deps: LayerDeps) -> str:
        return (await deferred_agent.run("load lazy", deps=deps)).output

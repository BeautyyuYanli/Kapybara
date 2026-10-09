"""Execute only the complete configuration fixed in this Workflow's input."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from kapy.capability_plugins import PluginDeps
    from kapy.capability_plugins.examples.temporal_agent import agent


@workflow.defn
class PluginExampleWorkflow:
    @workflow.run
    async def run(self, prompt: str, deps: PluginDeps) -> str:
        result = await agent.run(prompt, deps=deps)
        return result.output

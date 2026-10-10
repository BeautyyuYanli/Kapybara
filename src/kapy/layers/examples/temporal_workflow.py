"""Run the JSON composition fixed in Workflow input, with no database ownership."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from kapy.layers import LayerDeps
    from kapy.layers.examples.temporal_agent import agent


@workflow.defn
class LayerExampleWorkflow:
    @workflow.run
    async def run(self, prompt: str, deps: LayerDeps) -> str:
        return (await agent.run(prompt, deps=deps)).output

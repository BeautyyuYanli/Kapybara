"""One Temporal execution runs one fresh Agent conversation and returns its output."""

from typing import cast

from pydantic_ai.settings import ModelSettings
from temporalio import workflow

from .agent import MODEL_ID, agent
from .types import RunnerInput


@workflow.defn
class RunnerWorkflow:
    @workflow.run
    async def run(self, data: RunnerInput) -> str:
        result = await agent.run(
            data.user_prompt,
            model=MODEL_ID,
            model_settings=cast(ModelSettings, data.config.model_settings),
            deps=data.config,
        )
        return result.output

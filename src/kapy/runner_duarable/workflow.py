"""Run one fresh conversation, persist its final snapshot, then return its output."""

from datetime import timedelta
from typing import cast

from pydantic_ai.settings import ModelSettings
from temporalio import workflow

from .agent import MODEL_ID, agent
from .types import RunnerInput, SaveRunnerStateInput


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
        await workflow.execute_activity(
            "kapy.save_runner_state",
            SaveRunnerStateInput(
                session_id=data.session_id,
                expected_version=data.runner_state_version,
                runner_state=result.all_messages_json().decode("utf-8"),
            ),
            start_to_close_timeout=timedelta(seconds=30),
        )
        return result.output

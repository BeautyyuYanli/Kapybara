"""Resume the supplied message snapshot, record history, then save the final state."""

from datetime import timedelta
from typing import cast

from pydantic import ValidationError
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.settings import ModelSettings
from temporalio import workflow

from .agent import MODEL_ID, agent
from .types import RunnerDeps, RunnerInput, SaveRunnerStateInput


@workflow.defn
class RunnerWorkflow:
    @workflow.run
    async def run(self, data: RunnerInput) -> str:
        try:
            history = (
                []
                if data.runner_state is None
                else ModelMessagesTypeAdapter.validate_json(data.runner_state)
            )
        except ValidationError as exc:
            # UserError fails the execution under PydanticAIPlugin instead of
            # indefinitely retrying a Workflow Task with the same bad input.
            raise UserError("Invalid runner_state message history") from exc
        result = await agent.run(
            data.user_prompt,
            model=MODEL_ID,
            model_settings=cast(ModelSettings, data.config.model_settings),
            deps=RunnerDeps(config=data.config, session_id=data.session_id),
            message_history=history,
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

"""Exercise Kapy State boundaries with an installed native CombinedCapability."""

from datetime import timedelta

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from pydantic_ai import Agent
    from pydantic_ai.durable_exec.temporal import TemporalDurability
    from pydantic_ai.messages import (
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )
    from pydantic_ai.models.function import FunctionModel
    from runner_duarable import nested_layer
    from temporalio import activity

    from kapy.layers import layers_capability, snapshot_layers
    from kapy.layers.examples import echo
    from kapy.runner_duarable.context import RunnerActivityContext
    from kapy.runner_duarable.layers import LayerStateCapability, return_layer_state
    from kapy.runner_duarable.types import RunnerDeps, SaveRunnerStateInput

    def model(messages, info):
        assert activity.in_activity()
        assert "CONTAINER_RULE" in (info.instructions or "")
        returns = [
            part
            for message in messages
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            assert "echo" in {tool.name for tool in info.function_tools}
            assert "lazy_echo" not in {tool.name for tool in info.function_tools}
            return ModelResponse(
                parts=[ToolCallPart("echo", {"text": text}) for text in ("A", "B", "C")]
            )
        assert [part.metadata for part in returns] == [
            None,
            ["application", {"state": 123}],
            None,
        ]
        assert any(
            isinstance(part, UserPromptPart) and part.content == "native-extra"
            for message in messages
            for part in message.parts
        )
        return ModelResponse(parts=[TextPart(";".join(str(part.content) for part in returns))])

    registry = {"nested": nested_layer, "other": echo}
    entrance = layers_capability(registry, wrap_toolset=return_layer_state)
    agent = Agent(
        FunctionModel(model),
        name="nested_state_test",
        deps_type=RunnerDeps,
        capabilities=[
            entrance,
            LayerStateCapability(),
            TemporalDurability(run_context_type=RunnerActivityContext),
        ],
    )


@workflow.defn
class NestedWorkflow:
    @workflow.run
    async def run(self, deps: RunnerDeps) -> str:
        result = await agent.run("echo three times", deps=deps)
        await workflow.execute_activity(
            "kapy.save_runner_state",
            SaveRunnerStateInput(
                session_id=deps.session_id,
                expected_version=0,
                runner_state=result.all_messages_json().decode(),
                layer_states=snapshot_layers(deps, registry),
            ),
            start_to_close_timeout=timedelta(seconds=30),
        )
        return result.output

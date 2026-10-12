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

    from kapy.runner_duarable.types import SaveRunnerStateInput
    from pydantic_ai_layer_protocol.examples import echo
    from pydantic_ai_layer_protocol.temporal import (
        LAYER_STATE_OWNER_KEY,
        TemporalLayerRegistry,
    )

    from .nested_layer import Deps, get_layers

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
            definition = next(tool for tool in info.function_tools if tool.name == "echo")
            assert definition.capability_id == "nested.tool"
            assert definition.metadata == {LAYER_STATE_OWNER_KEY: "nested"}
            assert definition.sequential is True
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

    layers = TemporalLayerRegistry(
        {"nested": nested_layer.make, "other": echo.make}, get_layers=get_layers
    )
    agent = Agent(
        FunctionModel(model),
        name="nested_state_test",
        deps_type=Deps,
        capabilities=[
            layers,
            TemporalDurability(),
        ],
    )


@workflow.defn
class NestedWorkflow:
    @workflow.run
    async def run(self, deps: Deps) -> str:
        result = await agent.run("echo three times", deps=deps)
        await workflow.execute_activity(
            "kapy.save_runner_state",
            SaveRunnerStateInput(
                session_id=deps.session_id,
                expected_version=0,
                runner_state=result.all_messages_json().decode(),
                layer_states=layers.snapshot(deps),
            ),
            start_to_close_timeout=timedelta(seconds=30),
        )
        return result.output

"""Resume the supplied message snapshot, record history, then save the final state."""

import asyncio
from datetime import timedelta
from typing import cast

from pydantic import ValidationError
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.settings import ModelSettings
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, is_cancelled_exception
from temporalio.workflow import ActivityCancellationType

from kapy.layers import load_layers, snapshot_layers

# Definitions are process-local and immutable; do not re-import MCP transports
# and their native dependencies while reconstructing a Workflow sandbox.
with workflow.unsafe.imports_passed_through():
    from kapy.layers.mcps import Config as MCPConfig
    from kapy.layers.mcps.models import GatewayEndpoint
    from kapy.layers.registry import LAYERS, get_layers

    from .mcp_gateway import READY_SIGNAL, hold_mcp_gateway

from .agent import MODEL_ID, agent
from .types import RunnerDeps, RunnerInput, SaveRunnerStateInput


@workflow.defn
class RunnerWorkflow:
    def __init__(self) -> None:
        self._mcp_gateway: GatewayEndpoint | None = None
        self._deps: RunnerDeps | None = None

    @workflow.signal(name=READY_SIGNAL)
    def mcp_gateway_ready(self, endpoint: GatewayEndpoint) -> None:
        """Record initialization/recovery; late older owner attempts cannot replace it."""
        if self._mcp_gateway is not None and endpoint.attempt < self._mcp_gateway.attempt:
            return
        self._mcp_gateway = endpoint
        if self._deps is not None:
            self._deps.mcp_gateway = endpoint

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
        try:
            layers = load_layers(
                {name: entry["config"] for name, entry in data.layers.items()},
                {name: entry["state"] for name, entry in data.layers.items()},
                LAYERS,
            )
        except ValueError as exc:
            raise UserError("Invalid layer configuration or state") from exc
        deps = RunnerDeps(config=data.config, session_id=data.session_id, layers=layers)
        self._deps = deps
        owner = None
        if "mcps" in layers and workflow.patched("kapy-mcp-run-gateway-v1"):
            owner = workflow.start_activity(
                hold_mcp_gateway,
                MCPConfig.model_validate(layers["mcps"]["config"]),
                task_queue=workflow.info().task_queue + "-mcp",
                start_to_close_timeout=timedelta(days=1),
                heartbeat_timeout=timedelta(seconds=10),
                cancellation_type=ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        try:
            if owner is not None:
                await workflow.wait_condition(lambda: self._mcp_gateway is not None or owner.done())
                if self._mcp_gateway is None:
                    await owner
                    raise UserError("MCP Gateway owner ended before initialization")
                deps.mcp_gateway = self._mcp_gateway
            result = await agent.run(
                data.user_prompt,
                model=MODEL_ID,
                model_settings=cast(ModelSettings, data.config.model_settings),
                deps=deps,
                message_history=history,
            )
        finally:
            if owner is not None:
                owner.cancel()
                try:
                    await owner
                except asyncio.CancelledError:
                    pass
                except ActivityError as exc:
                    if not is_cancelled_exception(exc):
                        raise
        try:
            layer_states = snapshot_layers(deps, LAYERS, get_layers)
        except ValueError as exc:
            raise UserError("Invalid final layer state") from exc
        await workflow.execute_activity(
            "kapy.save_runner_state",
            SaveRunnerStateInput(
                session_id=data.session_id,
                expected_version=data.runner_state_version,
                runner_state=result.all_messages_json().decode("utf-8"),
                layer_states=layer_states,
            ),
            start_to_close_timeout=timedelta(seconds=30),
        )
        return result.output

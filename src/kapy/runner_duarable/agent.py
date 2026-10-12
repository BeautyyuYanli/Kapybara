"""Temporal registration and per-run model resolution without a model catalogue.

Resolution opens and closes the existing Provider/Model contexts to obtain a fully
initialized SDK model. This preserves protocol-specific profile and preparation
behavior in Workflow code. The SDK subsequently re-enters and closes model
contexts on both the Workflow and Activity sides; providers recreate their owned
HTTP clients on re-entry. The Workflow-side context remains open while awaiting
the Activity. Constructors must perform no external I/O, as with the SDK's own
Workflow-side model inference. HTTP requests only execute inside Activities.
"""

from datetime import timedelta

from pydantic_ai import Agent
from pydantic_ai.capabilities import CapabilityOrdering, Hooks, ResolveModelId
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import Model, ModelResolutionContext
from pydantic_ai.toolsets import AbstractToolset, DynamicToolset
from shellctl.shared.constants import SHELL_TOOL_HARD_TIMEOUT_SECONDS
from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.workflow import ActivityCancellationType, ActivityConfig

from kapy.control.models.runtime import build_model, build_provider, resolve_classes
from kapy.layers.registry import layer_registry

from .context import RunnerActivityContext
from .output import handle_deltas
from .recording import MessageRecordCapability, Recorder
from .types import RunnerDeps

MODEL_ID = "kapy-configured"


async def resolve_model(ctx: ModelResolutionContext[RunnerDeps], model_id: str) -> Model | None:
    if model_id != MODEL_ID:
        return None
    try:
        config = ctx.deps.config
        provider_config = config.provider_config()
        # Class imports select installed code, not Workflow state. Do not re-import
        # SDK providers and their transitive dependencies inside the sandbox.
        with workflow.unsafe.imports_passed_through():
            provider_class, model_class = resolve_classes(provider_config)
        async with (
            build_provider(provider_class, provider_config) as provider,
            build_model(
                model_class,
                config.model_name,
                provider,
                profile={"context_window": config.context_window},
            ) as model,
        ):
            return model
    except (ValueError, TypeError) as exc:
        # Bad configuration must fail the execution, not endlessly retry a
        # Workflow Task. UserError is classified by the SDK Temporal plugin.
        raise UserError("Invalid model execution configuration") from exc


def _layer_activity_configs() -> dict[str, ActivityConfig]:
    policy: ActivityConfig = {
        "start_to_close_timeout": timedelta(seconds=SHELL_TOOL_HARD_TIMEOUT_SECONDS + 60),
        "retry_policy": RetryPolicy(maximum_attempts=1),
        "heartbeat_timeout": timedelta(seconds=10),
        "cancellation_type": ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
    }
    configs: dict[str, ActivityConfig] = {}

    def configure(toolset: AbstractToolset[RunnerDeps]) -> None:
        if isinstance(toolset, DynamicToolset):
            assert toolset.id is not None
            configs[toolset.id] = policy

    toolset = layer_registry.get_toolset()
    if isinstance(toolset, AbstractToolset):
        toolset.apply(configure)
    return configs


recorder = Recorder()
agent = Agent(
    name="runner_duarable",
    model=MODEL_ID,
    deps_type=RunnerDeps,
    capabilities=[
        MessageRecordCapability(recorder),
        ResolveModelId(resolve_model),
        layer_registry,
        Hooks(
            before_model_request=recorder.predict_response_seq,
            ordering=CapabilityOrdering(
                position="innermost",
                requires=[MessageRecordCapability],
            ),
        ),
        TemporalDurability(
            run_context_type=RunnerActivityContext,
            event_stream_handler=handle_deltas,
            model_activity_config={
                "start_to_close_timeout": timedelta(minutes=5),
                # Temporal counts the initial attempt, so two attempts allow one retry.
                "retry_policy": RetryPolicy(maximum_attempts=2),
            },
            toolset_activity_config=_layer_activity_configs(),
        ),
    ],
)
"""Register this Agent once through the SDK's AgentPlugin on the Worker."""

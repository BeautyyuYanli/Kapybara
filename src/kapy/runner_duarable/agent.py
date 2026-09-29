"""Temporal registration and per-run model resolution without a model catalogue.

Resolution opens and closes the existing Provider/Model contexts to obtain a fully
initialized SDK model. This preserves protocol-specific profile and preparation
behavior in Workflow code. The SDK subsequently re-enters and closes model
contexts on both the Workflow and Activity sides; providers recreate their owned
HTTP clients on re-entry. The Workflow-side context remains open while awaiting
the Activity. Constructors must perform no external I/O, as with the SDK's own
Workflow-side model inference. HTTP requests only execute inside Activities.
"""

from pydantic_ai import Agent
from pydantic_ai.capabilities import ResolveModelId
from pydantic_ai.durable_exec.temporal import TemporalDurability
from pydantic_ai.exceptions import UserError
from pydantic_ai.models import Model, ModelResolutionContext
from temporalio import workflow

from kapy.control.models.runtime import build_model, build_provider, resolve_classes

from .history import HistoryRecordCapability
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


agent = Agent(
    name="runner_duarable",
    model=MODEL_ID,
    deps_type=RunnerDeps,
    capabilities=[TemporalDurability(), ResolveModelId(resolve_model), HistoryRecordCapability()],
)
"""Register this Agent once through the SDK's AgentPlugin on the Worker."""

"""Process-local static Agent; each Activity supplies its own model and writable deps.

Factories perform no I/O. Per-run native SDK scopes own tool resources; only the
Activity constructs and closes its model and provider.
"""

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import CapabilityOrdering, Hooks
from pydantic_ai.capabilities.abstract import ValidatedToolArgs
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from kapy.layers.registry import layer_registry
from kapy.lifecycle import LifecycleStatus

from .recording import MessageRecordCapability, Recorder
from .scopes import check_owner
from .types import RunnerDeps


async def before_request(
    ctx: RunContext[RunnerDeps], request: ModelRequestContext
) -> ModelRequestContext:
    await check_owner(ctx.deps.session_factory, ctx.deps.activity_id, (LifecycleStatus.RUNNING,))
    return await recorder.predict_response_seq(ctx, request)


async def before_tool(
    ctx: RunContext[RunnerDeps],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: ValidatedToolArgs,
) -> ValidatedToolArgs:
    await check_owner(ctx.deps.session_factory, ctx.deps.activity_id, (LifecycleStatus.RUNNING,))
    return args


recorder = Recorder()
agent = Agent(
    name="runner",
    deps_type=RunnerDeps,
    capabilities=[
        MessageRecordCapability(recorder),
        layer_registry,
        Hooks(
            before_model_request=before_request,
            before_tool_execute=before_tool,
            ordering=CapabilityOrdering(
                position="innermost",
                requires=[MessageRecordCapability],
            ),
        ),
    ],
)

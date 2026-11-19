"""Process-local static Agent; each Activity supplies its own model and writable deps.

Factories perform no I/O. The SDK owns Toolset lifecycles; the Activity supplies
its model, provider and opened layer handles.
"""

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import CapabilityOrdering, Hooks
from pydantic_ai.capabilities.abstract import ValidatedToolArgs
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import ToolDefinition

from kapy.layers.registry import layer_registry

from .recording import Recorder, RunnerPersistenceCapability, check_owner
from .types import RunnerDeps


async def before_request(
    ctx: RunContext[RunnerDeps], request: ModelRequestContext
) -> ModelRequestContext:
    await check_owner(ctx.deps)
    return await recorder.predict_response_seq(ctx, request)


async def before_tool(
    ctx: RunContext[RunnerDeps],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: ValidatedToolArgs,
) -> ValidatedToolArgs:
    await check_owner(ctx.deps)
    return args


recorder = Recorder()
agent = Agent(
    name="runner",
    deps_type=RunnerDeps,
    capabilities=[
        RunnerPersistenceCapability(recorder),
        layer_registry,
        Hooks(
            before_model_request=before_request,
            before_tool_execute=before_tool,
            ordering=CapabilityOrdering(
                position="innermost",
                requires=[RunnerPersistenceCapability],
            ),
        ),
    ],
)

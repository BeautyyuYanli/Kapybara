"""Process-local static Agent; each Activity supplies its own model and writable deps.

Factories perform no I/O. Per-run native SDK scopes own tool resources; only the
Activity constructs and closes its model and provider.
"""

from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    CapabilityOrdering,
    Hooks,
)
from pydantic_ai.capabilities.abstract import ValidatedToolArgs
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.output import OutputContext
from pydantic_ai.tools import ToolDefinition

from kapy.layers.registry import layer_registry

from .ownership import OwnershipCapability, check
from .recording import MessageRecordCapability
from .types import RunnerDeps


async def before_request(
    ctx: RunContext[RunnerDeps], request: ModelRequestContext
) -> ModelRequestContext:
    request = await ctx.deps.recorder.before_model_request(ctx, request)
    # Initial persistence/publication awaits too; fence the physical model call
    # after that boundary, not only entry to this innermost callback.
    await check(ctx)
    return request


async def before_tool(
    ctx: RunContext[RunnerDeps],
    *,
    call: ToolCallPart,
    tool_def: ToolDefinition,
    args: ValidatedToolArgs,
) -> ValidatedToolArgs:
    await check(ctx)
    ctx.deps.recorder.validate_messages(ctx.messages)
    return args


async def before_output(ctx: RunContext[RunnerDeps], *, output, output_context: OutputContext):
    # The outer operation wrapper runs before awaited preparation hooks. This
    # final hook gates the actual output function, including handler re-entry.
    await check(ctx)
    return output


agent = Agent(
    name="runner",
    deps_type=RunnerDeps,
    capabilities=[
        MessageRecordCapability(),
        OwnershipCapability(),
        layer_registry,
        Hooks(
            before_model_request=before_request,
            before_tool_execute=before_tool,
            before_output_process=before_output,
            ordering=CapabilityOrdering(
                position="innermost",
                requires=[MessageRecordCapability],
                # Remain last when a business hook also requests innermost.
                wrapped_by=[AbstractCapability],
            ),
        ),
    ],
)

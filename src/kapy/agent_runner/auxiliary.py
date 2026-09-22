"""Normal SDK auxiliary runs with optional, model-definition-preserving constraints.

These runs borrow the stable Agent and execution capabilities. They never receive
SessionExecutionCapability or the runner's business output publisher. History is
copied once; retry feedback is owned by the SDK's temporary graph.
Run metadata marks these calls so business-only capabilities can skip them.
"""

import json
from collections.abc import Sequence
from copy import deepcopy
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ValidationError
from pydantic_ai import Agent, RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    CapabilityOrdering,
    OutputContext,
    WrapModelRequestHandler,
    WrapOutputProcessHandler,
)
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.messages import ModelMessage, ModelResponse
from pydantic_ai.models import ModelRequestContext


class SoftOutputCapability(AbstractCapability[Any]):
    """Validate JSON text without replacing SDK output tools or model output schema."""

    def __init__(self, result_type: type[BaseModel]) -> None:
        self.result_type = result_type

    async def wrap_output_process(
        self,
        ctx: RunContext[Any],
        *,
        output_context: OutputContext,
        output: Any,
        handler: WrapOutputProcessHandler,
    ) -> BaseModel:
        if not isinstance(output, str):
            raise ModelRetry("Return the requested JSON object as text.")
        try:
            return self.result_type.model_validate_json(output, strict=True)
        except ValidationError as error:
            errors = [
                {"path": item["loc"], "reason": item["msg"]}
                for item in error.errors(include_url=False, include_input=False)
            ]
            raise ModelRetry("Invalid JSON output: " + json.dumps(errors)) from None


class BlockOtherToolsCapability(AbstractCapability[Any]):
    """Reject client calls through SDK retries before argument validation/execution."""

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="innermost")

    async def before_tool_validate(self, ctx, *, call, tool_def, args):
        raise ModelRetry("Tools are not allowed for this task. Complete it directly as text.")

    async def wrap_model_request(
        self,
        ctx: RunContext[Any],
        *,
        request_context: ModelRequestContext,
        handler: WrapModelRequestHandler,
    ) -> ModelResponse:
        # Native tools execute inside the provider request, before a client hook
        # could reject them. Check prepared definitions before making that request.
        if request_context.model_request_parameters.native_tools:
            raise ValueError("block_other_tools does not support native server tools")
        return await handler(request_context)


async def run_auxiliary(
    agent: Agent[Any, Any],
    prompt: str,
    *,
    history: Sequence[ModelMessage],
    session_id: UUID,
    deps: Any,
    capabilities: Sequence[AbstractCapability[Any]],
    result_type: type[BaseModel] | None = None,
    block_other_tools: bool = False,
) -> Any:
    extra = list(capabilities)
    if result_type is not None:
        if not isinstance(result_type, type) or not issubclass(result_type, BaseModel):
            raise TypeError("result_type must be a Pydantic BaseModel subclass")
        schema = result_type.model_json_schema(mode="validation")
        prompt += "\n\nReturn JSON matching this schema:\n" + json.dumps(schema)
        extra.append(SoftOutputCapability(result_type))
    if block_other_tools:
        extra.append(BlockOtherToolsCapability())
    result = await agent.run(
        prompt,
        message_history=deepcopy(list(history)),
        conversation_id=str(session_id),
        deps=deps,
        capabilities=extra,
        metadata={"kapy_run_kind": "auxiliary"},
        retries={"tools": 2, "output": 2},
    )
    return result.output

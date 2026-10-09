"""Compose code-mode tools with a policy for the actual model request.

The policy replaces all collected instruction parts, including static instructions
and CodeMode's dynamic catalog. It filters only request ``function_tools``; SDK
tool registration and execution, output tools, native tools, and message content
(including ``SystemPromptPart``) keep their SDK semantics. The final before hook
runs inside the SDK's wrap chain; the SDK records rewritten instructions on the
current request and uses the rewritten parameters for optional token estimates.
"""

from collections.abc import Collection
from copy import deepcopy
from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import (
    AbstractCapability,
    CapabilityOrdering,
    CombinedCapability,
)
from pydantic_ai.messages import InstructionPart
from pydantic_ai.models import ModelRequestContext
from pydantic_ai_harness import CodeMode


class _ModelRequestPolicyCapability(AbstractCapability[Any]):
    """Apply saved instructions and a name whitelist without mutable run state."""

    def __init__(
        self, instructions: list[InstructionPart] | None, tool_whitelist: frozenset[str]
    ) -> None:
        self._instructions = deepcopy(instructions) if instructions is not None else []
        self._tool_whitelist = tool_whitelist

    def get_ordering(self) -> CapabilityOrdering:
        return CapabilityOrdering(position="innermost", wrapped_by=[AbstractCapability])

    async def before_model_request(
        self,
        ctx: RunContext[Any],
        request_context: ModelRequestContext,
    ) -> ModelRequestContext:
        """Rewrite once, after wrap-input and other before-request hooks.

        An explicit empty list expresses clearing outgoing instruction parts.
        Fresh instruction copies isolate configured parts from each request.
        """
        parameters = request_context.model_request_parameters
        return replace(
            request_context,
            model_request_parameters=replace(
                parameters,
                instruction_parts=deepcopy(self._instructions),
                function_tools=[
                    tool for tool in parameters.function_tools if tool.name in self._tool_whitelist
                ],
            ),
        )


class AgentBehaviorCapability(CombinedCapability[Any]):
    """Use code mode while fixing outgoing instructions and direct tool exposure.

    ``instructions`` are preconfigured SDK parts, copied at construction and for
    each request. Their order and fields, including ``dynamic``, are preserved;
    ``None`` and ``[]`` clear outgoing instructions.

    ``tool_whitelist`` matches final SDK tool names exactly, including prefixes.
    Whitelisted tools stay direct and are excluded from CodeMode's sandbox.
    ``run_code`` is always added; other eligible tools remain callable through it.
    The SDK owns child binding and CodeMode's per-run state, so the capability can
    be registered on an Agent or passed to an individual run.
    Reuse SDK-produced history from the same Agent with this capability enabled
    and the same configured instructions.
    """

    def __init__(
        self, instructions: list[InstructionPart] | None, *, tool_whitelist: Collection[str]
    ) -> None:
        whitelist = frozenset(tool_whitelist) | {"run_code"}
        # CombinedCapability flattens children: request hooks belong to the leaf.
        super().__init__(
            [
                CodeMode[Any](
                    dynamic_catalog=True,
                    tools=lambda ctx, tool: tool.name not in whitelist,
                ),
                _ModelRequestPolicyCapability(instructions, whitelist),
            ]
        )

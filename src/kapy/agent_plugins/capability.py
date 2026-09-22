"""Trusted Pydantic AI adapter; builtin functions keep native SDK argument validation."""

from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability, Capability, PrefixTools
from pydantic_ai.models import ModelRequestContext
from pydantic_ai.tools import Tool

from .contracts import PluginBinding
from .registry import check_name
from .service import ScopedStateStore, _read_only


class _PluginCapability(Capability[Any]):
    """Declarative contribution with name validation after SDK toolset composition."""

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        # SDK wrapper toolsets run after prepare_tools and can rename tools.
        # Check the resulting request definitions; SDK composition owns collisions.
        for tool in request_context.model_request_parameters.function_tools:
            check_name(tool.name)
            if len(tool.name) > 64:
                raise ValueError(f"Plugin tool name exceeds 64 characters: {tool.name}")
        return request_context


class PluginCapabilityAdapter:
    @staticmethod
    def build(
        provider: str, name: str, binding: PluginBinding, store: ScopedStateStore, names: set[str]
    ) -> tuple[AbstractCapability[Any], ...]:
        """Normalize declarative contributions, then retain native capabilities as-is."""

        def check_call(_ctx: RunContext[Any], /, **kwargs: Any) -> None:
            # The SDK validates typed arguments before this hook and owns sync,
            # async and sync-returning-awaitable dispatch. Keep its semantics.
            store.check_active()

        tools = []
        for declaration in binding.tools:
            check_name(declaration.name)
            final = f"{provider}_{name}_{declaration.name}"
            if len(final) > 64 or final in names:
                raise ValueError(f"Invalid or duplicate plugin tool name {final}")
            names.add(final)
            tools.append(
                Tool(
                    declaration.function,
                    name=declaration.name,
                    description=declaration.description,
                    takes_ctx=False,
                    args_validator=check_call,
                )
            )

        async def instructions() -> str:
            store.check_active()
            token = _read_only.set(True)
            try:
                return await binding.instructions() if binding.instructions is not None else ""
            finally:
                _read_only.reset(token)

        return (
            PrefixTools(
                wrapped=_PluginCapability(
                    id=f"{provider}.{name}",
                    instructions=instructions if binding.instructions else None,
                    tools=tools,
                ),
                prefix=f"{provider}_{name}",
            ),
            *binding.capabilities,
        )

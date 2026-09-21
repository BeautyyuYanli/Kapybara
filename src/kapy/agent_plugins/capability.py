"""Trusted Pydantic AI adapter; builtin functions keep native SDK argument validation."""

from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability, PrefixTools
from pydantic_ai.tools import Tool

from .contracts import PluginBinding
from .registry import check_name
from .service import ScopedStateStore, _read_only


class PluginCapabilityAdapter:
    @staticmethod
    def build(
        provider: str, name: str, binding: PluginBinding, store: ScopedStateStore, names: set[str]
    ) -> PrefixTools[Any]:
        async def check_call(_ctx: RunContext[Any], /, **kwargs: Any) -> None:
            # The SDK validates typed arguments before this hook and owns sync,
            # async and sync-returning-awaitable dispatch. Keep its semantics.
            await store.check()

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
            await store.check()
            token = _read_only.set(True)
            try:
                return await binding.instructions() if binding.instructions is not None else ""
            finally:
                _read_only.reset(token)

        return PrefixTools(
            wrapped=Capability(
                id=f"{provider}.{name}",
                instructions=instructions if binding.instructions else None,
                tools=tools,
            ),
            prefix=f"{provider}_{name}",
        )

"""Composite context provider that aggregates child providers.

This provider has no domain behavior of its own. Instead it composes child
providers, aggregates their list-like interfaces, and dispatches qualified tool
names back to the owning provider.

Qualified tool naming uses `<provider_name>.<tool_name>` so runtime callers can
work with one flat tool namespace while each provider still defines local tool
names independently.
"""

from __future__ import annotations

from typing import Any, Self

from k.agent.context_providers.base import (
    ContextProvider,
    GroundingItem,
    ProviderConfig,
    ToolSpec,
    TurnContext,
)
from k.agent.context_providers.memory import MemoryProvider, MemoryProviderConfig
from k.agent.context_providers.preferences import (
    PreferencesProvider,
    PreferencesProviderConfig,
)
from k.agent.context_providers.skills import SkillsProvider, SkillsProviderConfig


class CompositeContextProviderConfig(ProviderConfig):
    """Static configuration for the built-in composite provider set."""

    memory: MemoryProviderConfig | None = None
    preferences: PreferencesProviderConfig | None = None
    skills: SkillsProviderConfig | None = None


class CompositeContextProvider(ContextProvider[CompositeContextProviderConfig]):
    """Aggregate child providers into one `ContextProvider` surface."""

    name = "context"
    config_model = CompositeContextProviderConfig

    providers: tuple[ContextProvider[Any], ...]

    def __init__(self, providers: list[ContextProvider[Any]]) -> None:
        self.providers = tuple(providers)
        self._validate_provider_names()

    @classmethod
    def from_config(cls, config: CompositeContextProviderConfig) -> Self:
        providers: list[ContextProvider[Any]] = []
        if config.preferences is not None:
            providers.append(PreferencesProvider.from_config(config.preferences))
        if config.memory is not None:
            providers.append(MemoryProvider.from_config(config.memory))
        if config.skills is not None:
            providers.append(SkillsProvider.from_config(config.skills))
        return cls(providers)

    def supports(self, ctx: TurnContext) -> bool:
        return True

    def priority(self, ctx: TurnContext) -> int:
        return 0

    def build_grounding(self, ctx: TurnContext) -> list[GroundingItem]:
        return [
            item
            for provider in self._active_providers(ctx)
            for item in provider.build_grounding(ctx)
        ]

    def build_finalization_grounding(self, ctx: TurnContext) -> list[GroundingItem]:
        return [
            item
            for provider in self._active_providers(ctx)
            for item in provider.build_finalization_grounding(ctx)
        ]

    def list_tools(self) -> list[ToolSpec]:
        qualified_tools: list[ToolSpec] = []
        seen_names: set[str] = set()
        for provider in self.providers:
            for spec in self._qualified_tools_for_provider(provider):
                if spec.name in seen_names:
                    raise ValueError(f"Duplicate qualified tool name: {spec.name!r}")
                seen_names.add(spec.name)
                qualified_tools.append(spec)
        return qualified_tools

    def runtime_tool_names(self, ctx: TurnContext) -> list[str]:
        return [
            self._qualified_tool_name(provider, local_name)
            for provider in self._active_providers(ctx)
            for local_name in self._validated_phase_names(
                provider, ctx, phase="runtime"
            )
        ]

    def finalization_tool_names(self, ctx: TurnContext) -> list[str]:
        return [
            self._qualified_tool_name(provider, local_name)
            for provider in self._active_providers(ctx)
            for local_name in self._validated_phase_names(
                provider,
                ctx,
                phase="finalization",
            )
        ]

    def execute_tool(self, name: str, payload: object, ctx: TurnContext) -> object:
        provider_name, local_name = self._split_qualified_tool_name(name)
        for provider in self._active_providers(ctx):
            if provider.name == provider_name:
                return provider.execute_tool(local_name, payload, ctx)
        raise ValueError(f"Unknown tool: {name!r}")

    def _active_providers(self, ctx: TurnContext) -> list[ContextProvider[Any]]:
        providers = [provider for provider in self.providers if provider.supports(ctx)]
        providers.sort(key=lambda provider: (provider.priority(ctx), provider.name))
        return providers

    def _qualified_tools_for_provider(
        self,
        provider: ContextProvider[Any],
    ) -> list[ToolSpec]:
        seen_names: set[str] = set()
        out: list[ToolSpec] = []
        for spec in provider.list_tools():
            if not spec.name or "." in spec.name:
                raise ValueError(
                    f"Provider {provider.name!r} declared an invalid local tool name: {spec.name!r}"
                )
            if spec.name in seen_names:
                raise ValueError(
                    f"Provider {provider.name!r} declared duplicate local tool name: {spec.name!r}"
                )
            seen_names.add(spec.name)
            out.append(
                ToolSpec(
                    name=self._qualified_tool_name(provider, spec.name),
                    description=spec.description,
                    input_schema=spec.input_schema,
                )
            )
        return out

    def _validated_phase_names(
        self,
        provider: ContextProvider[Any],
        ctx: TurnContext,
        *,
        phase: str,
    ) -> list[str]:
        declared = {spec.name for spec in provider.list_tools()}
        names = (
            provider.runtime_tool_names(ctx)
            if phase == "runtime"
            else provider.finalization_tool_names(ctx)
        )
        seen_names: set[str] = set()
        for name in names:
            if name not in declared:
                raise ValueError(
                    f"Provider {provider.name!r} exposed undeclared {phase} tool {name!r}"
                )
            if name in seen_names:
                raise ValueError(
                    f"Provider {provider.name!r} exposed duplicate {phase} tool {name!r}"
                )
            seen_names.add(name)
        return names

    def _validate_provider_names(self) -> None:
        seen_names: set[str] = set()
        for provider in self.providers:
            if provider.name in seen_names:
                raise ValueError(
                    f"Duplicate provider name in composite: {provider.name!r}"
                )
            seen_names.add(provider.name)

    @staticmethod
    def _qualified_tool_name(
        provider: ContextProvider[Any],
        local_name: str,
    ) -> str:
        return f"{provider.name}.{local_name}"

    @staticmethod
    def _split_qualified_tool_name(name: str) -> tuple[str, str]:
        provider_name, sep, local_name = name.partition(".")
        if not provider_name or not sep or not local_name:
            raise ValueError(
                "Qualified tool names must be in '<provider_name>.<tool_name>' form"
            )
        return provider_name, local_name

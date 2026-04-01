"""Shared abstractions and built-in turn-scoped context providers."""

from k.agent.context_providers.base import (
    ContextProvider,
    GroundingItem,
    ProviderConfig,
    ToolProvider,
    ToolSpec,
    TurnContext,
)
from k.agent.context_providers.composite import (
    CompositeContextProvider,
    CompositeContextProviderConfig,
)
from k.agent.context_providers.memory import MemoryProvider, MemoryProviderConfig
from k.agent.context_providers.preferences import (
    PreferencesProvider,
    PreferencesProviderConfig,
)
from k.agent.context_providers.skills import SkillsProvider, SkillsProviderConfig

__all__ = [
    "CompositeContextProvider",
    "CompositeContextProviderConfig",
    "ContextProvider",
    "GroundingItem",
    "MemoryProvider",
    "MemoryProviderConfig",
    "PreferencesProvider",
    "PreferencesProviderConfig",
    "ProviderConfig",
    "SkillsProvider",
    "SkillsProviderConfig",
    "ToolProvider",
    "ToolSpec",
    "TurnContext",
]

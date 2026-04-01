"""Runtime-agnostic abstractions for context and tool providers.

`ToolProvider` owns the execution-oriented surface area: a provider declares the
 complete set of tools it can execute and exposes a single dispatch entrypoint.

`ContextProvider` extends that base with turn participation, pre-run grounding,
post-run finalization grounding, and phase-specific tool selection. Tools are
declared once in `list_tools()`, while the runtime/finalization methods only
select subsets by local tool name to keep the declaration site authoritative.

`ProviderConfig` structures the validated config payload accepted by
`from_config()`, while each provider also exposes a fixed `name` and
`config_model` class attribute for routing and composition.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Self

from pydantic import BaseModel, ConfigDict


@dataclass(slots=True, frozen=True)
class TurnContext:
    """Minimal turn data every context provider can safely depend on."""

    in_channel: str
    out_channel: str | None
    contacts: tuple[str, ...]
    content: str


@dataclass(slots=True, frozen=True)
class GroundingItem:
    """One grounding chunk returned by a context provider."""

    content: str


@dataclass(slots=True, frozen=True)
class ToolSpec:
    """Structured tool declaration exposed by a provider."""

    name: str
    description: str
    input_schema: dict[str, Any]


class ProviderConfig(BaseModel):
    """Base model for provider-specific configuration payloads."""

    model_config = ConfigDict(extra="forbid")


class ToolProvider(ABC):
    """Base class for providers that define executable tools."""

    @abstractmethod
    def list_tools(self) -> list[ToolSpec]:
        """Return the complete set of tools defined by this provider."""

    @abstractmethod
    def execute_tool(self, name: str, payload: object, ctx: TurnContext) -> object:
        """Execute one provider-local tool using a runtime-supplied payload."""


class ContextProvider[ConfigT: ProviderConfig](ToolProvider, ABC):
    """Tool provider that also contributes turn-scoped grounding."""

    name: ClassVar[str]
    config_model: ClassVar[type[ProviderConfig]]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        if cls is ContextProvider:
            return
        name = getattr(cls, "name", None)
        if not isinstance(name, str) or not name or "." in name:
            raise TypeError(
                f"{cls.__name__} must define a non-empty provider name without dots"
            )
        config_model = getattr(cls, "config_model", None)
        if not isinstance(config_model, type) or not issubclass(
            config_model, ProviderConfig
        ):
            raise TypeError(
                f"{cls.__name__} must define config_model inheriting ProviderConfig"
            )

    @classmethod
    @abstractmethod
    def from_config(cls, config: ConfigT) -> Self:
        """Build one provider instance from validated provider config."""

    @abstractmethod
    def supports(self, ctx: TurnContext) -> bool:
        """Return whether this provider should participate in the current turn."""

    @abstractmethod
    def priority(self, ctx: TurnContext) -> int:
        """Return provider order where lower numbers run earlier."""

    @abstractmethod
    def build_grounding(self, ctx: TurnContext) -> list[GroundingItem]:
        """Return default grounding content for the start of the turn."""

    def build_finalization_grounding(self, ctx: TurnContext) -> list[GroundingItem]:
        """Return grounding content for the post-run finalization phase."""

        return []

    def runtime_tool_names(self, ctx: TurnContext) -> list[str]:
        """Return tool names exposed while the model is still deciding what to do."""

        return []

    def finalization_tool_names(self, ctx: TurnContext) -> list[str]:
        """Return tool names exposed after the main run, during finalization."""

        return []

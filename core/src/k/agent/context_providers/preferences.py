"""Preference-backed context provider.

This provider keeps the current preference-loading rules intact by reusing the
existing `<Preferences>` prompt builder. It exposes one runtime tool that can
re-run the same routed preference resolution with optional channel/contact
overrides, while intentionally leaving finalization empty until preference
writeback semantics are designed explicitly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Self

from pydantic import BaseModel, ConfigDict, field_validator

from k.agent.context_providers.base import (
    ContextProvider,
    GroundingItem,
    ProviderConfig,
    ToolSpec,
    TurnContext,
)
from k.agent.core.preference_injection import load_preferences_prompt


class PreferencesProviderConfig(ProviderConfig):
    """Configuration for `PreferencesProvider`."""

    config_base: Path
    preferences_root: Path | None = None

    @field_validator("config_base", "preferences_root")
    @classmethod
    def _normalize_paths(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        return value.expanduser().resolve()

    @property
    def resolved_preferences_root(self) -> Path:
        if self.preferences_root is not None:
            return self.preferences_root
        return self.config_base / "preferences"


class LoadPreferencesInput(BaseModel):
    """Load routed preferences, optionally overriding the current turn scope."""

    model_config = ConfigDict(extra="forbid")

    in_channel: str | None = None
    out_channel: str | None = None
    contacts: list[str] | None = None


class PreferencesProvider(ContextProvider[PreferencesProviderConfig]):
    """Adapt the current preference prompt loader into a structured provider."""

    name = "preferences"
    config_model = PreferencesProviderConfig

    config: PreferencesProviderConfig

    _PRIORITY = 100

    def __init__(self, *, config: PreferencesProviderConfig) -> None:
        self.config = config

    @classmethod
    def from_config(cls, config: PreferencesProviderConfig) -> Self:
        return cls(config=config)

    def supports(self, ctx: TurnContext) -> bool:
        return True

    def priority(self, ctx: TurnContext) -> int:
        return self._PRIORITY

    def build_grounding(self, ctx: TurnContext) -> list[GroundingItem]:
        prompt = load_preferences_prompt(
            in_channel=ctx.in_channel,
            contacts=list(ctx.contacts),
            pref_root=self.config.resolved_preferences_root,
            out_channel=ctx.out_channel,
        )
        if not prompt:
            return []
        return [GroundingItem(content=prompt)]

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="load_preferences",
                description="Load routed preferences for the current or overridden scope.",
                input_schema=LoadPreferencesInput.model_json_schema(),
            )
        ]

    def runtime_tool_names(self, ctx: TurnContext) -> list[str]:
        return ["load_preferences"]

    def execute_tool(self, name: str, payload: object, ctx: TurnContext) -> object:
        if name != "load_preferences":
            raise ValueError(f"Unknown preference tool: {name!r}")

        parsed = LoadPreferencesInput.model_validate(payload)
        return load_preferences_prompt(
            in_channel=parsed.in_channel or ctx.in_channel,
            contacts=parsed.contacts
            if parsed.contacts is not None
            else list(ctx.contacts),
            pref_root=self.config.resolved_preferences_root,
            out_channel=(
                parsed.out_channel
                if parsed.in_channel is not None or parsed.out_channel is not None
                else ctx.out_channel
            ),
        )

"""Tests for the structured provider abstractions."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from k.agent.context_providers import (
    CompositeContextProvider,
    CompositeContextProviderConfig,
    ContextProvider,
    GroundingItem,
    ProviderConfig,
    ToolSpec,
    TurnContext,
)


class _DemoProviderConfig(ProviderConfig):
    enabled: bool = True


class _LookupPayload(BaseModel):
    query: str


class _FinalizePayload(BaseModel):
    summary: str


class _DemoProvider(ContextProvider[_DemoProviderConfig]):
    name = "demo"
    config_model = _DemoProviderConfig

    config: _DemoProviderConfig
    finalized: list[str]

    def __init__(self, *, config: _DemoProviderConfig) -> None:
        self.config = config
        self.finalized = []

    @classmethod
    def from_config(cls, config: _DemoProviderConfig) -> _DemoProvider:
        return cls(config=config)

    def supports(self, ctx: TurnContext) -> bool:
        return self.config.enabled

    def priority(self, ctx: TurnContext) -> int:
        return 10

    def build_grounding(self, ctx: TurnContext) -> list[GroundingItem]:
        return [GroundingItem(content=ctx.content)]

    def build_finalization_grounding(self, ctx: TurnContext) -> list[GroundingItem]:
        return [GroundingItem(content=f"final:{ctx.content}")]

    def list_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="lookup_demo",
                description="Look up demo context.",
                input_schema=_LookupPayload.model_json_schema(),
            ),
            ToolSpec(
                name="persist_demo",
                description="Persist demo context.",
                input_schema=_FinalizePayload.model_json_schema(),
            ),
        ]

    def runtime_tool_names(self, ctx: TurnContext) -> list[str]:
        return ["lookup_demo"]

    def finalization_tool_names(self, ctx: TurnContext) -> list[str]:
        return ["persist_demo"]

    def execute_tool(self, name: str, payload: object, ctx: TurnContext) -> object:
        if name == "lookup_demo":
            parsed = _LookupPayload.model_validate(payload)
            return f"{ctx.in_channel}:{parsed.query}"
        if name == "persist_demo":
            parsed = _FinalizePayload.model_validate(payload)
            self.finalized.append(parsed.summary)
            return {"ok": True}
        raise ValueError(name)


def test_structured_provider_exposes_local_tools_and_phase_names() -> None:
    provider = _DemoProvider.from_config(_DemoProviderConfig())
    ctx = TurnContext(
        in_channel="telegram/chat/1",
        out_channel=None,
        contacts=("telegram/42",),
        content="hello",
    )

    assert provider.name == "demo"
    assert provider.build_grounding(ctx) == [GroundingItem(content="hello")]
    assert provider.build_finalization_grounding(ctx) == [
        GroundingItem(content="final:hello")
    ]
    assert provider.runtime_tool_names(ctx) == ["lookup_demo"]
    assert provider.finalization_tool_names(ctx) == ["persist_demo"]
    assert provider.list_tools() == [
        ToolSpec(
            name="lookup_demo",
            description="Look up demo context.",
            input_schema=_LookupPayload.model_json_schema(),
        ),
        ToolSpec(
            name="persist_demo",
            description="Persist demo context.",
            input_schema=_FinalizePayload.model_json_schema(),
        ),
    ]


def test_composite_provider_qualifies_and_dispatches_tool_names() -> None:
    composite = CompositeContextProvider(
        [
            _DemoProvider.from_config(_DemoProviderConfig()),
        ]
    )
    ctx = TurnContext(
        in_channel="telegram/chat/1",
        out_channel=None,
        contacts=(),
        content="hello",
    )

    assert composite.runtime_tool_names(ctx) == ["demo.lookup_demo"]
    assert composite.finalization_tool_names(ctx) == ["demo.persist_demo"]
    assert composite.list_tools() == [
        ToolSpec(
            name="demo.lookup_demo",
            description="Look up demo context.",
            input_schema=_LookupPayload.model_json_schema(),
        ),
        ToolSpec(
            name="demo.persist_demo",
            description="Persist demo context.",
            input_schema=_FinalizePayload.model_json_schema(),
        ),
    ]
    assert composite.execute_tool("demo.lookup_demo", {"query": "q"}, ctx) == (
        "telegram/chat/1:q"
    )
    assert composite.execute_tool("demo.persist_demo", {"summary": "done"}, ctx) == {
        "ok": True
    }


def test_composite_provider_rejects_undeclared_phase_tool_names() -> None:
    class _BadProvider(_DemoProvider):
        name = "bad"
        config_model = _DemoProviderConfig

        def runtime_tool_names(self, ctx: TurnContext) -> list[str]:
            return ["missing"]

    composite = CompositeContextProvider(
        [_BadProvider.from_config(_DemoProviderConfig())]
    )
    ctx = TurnContext(
        in_channel="telegram/chat/1",
        out_channel=None,
        contacts=(),
        content="hello",
    )

    with pytest.raises(ValueError, match="undeclared runtime tool"):
        composite.runtime_tool_names(ctx)


def test_composite_provider_from_config_collects_built_in_providers(
    tmp_path: Path,
) -> None:
    config_base = tmp_path / ".kapybara"
    composite = CompositeContextProvider.from_config(
        CompositeContextProviderConfig(
            memory={"config_base": config_base},
            preferences={"config_base": config_base},
            skills={"config_base": config_base},
        )
    )

    assert [provider.name for provider in composite.providers] == [
        "preferences",
        "memory",
        "skills",
    ]

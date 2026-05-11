"""Tests for built-in context provider implementations."""

from __future__ import annotations

from pathlib import Path

from k.agent.context_providers import (
    CompositeContextProvider,
    MemoryProvider,
    MemoryProviderConfig,
    PreferencesProvider,
    PreferencesProviderConfig,
    SkillsProvider,
    SkillsProviderConfig,
    TurnContext,
)
from k.agent.memory.entities import MemoryRecord


def test_memory_provider_builds_grounding_and_exposes_phase_tool_names(
    tmp_path: Path,
) -> None:
    config_base = tmp_path / ".kapybara"
    provider = MemoryProvider.from_config(MemoryProviderConfig(config_base=config_base))
    ctx = TurnContext(
        in_channel="telegram/chat/1",
        out_channel=None,
        contacts=("telegram/42",),
        content="hello",
    )

    assert provider.name == "memory"
    assert provider.build_grounding(ctx) == []
    assert provider.runtime_tool_names(ctx) == [
        "list_latest_memories",
        "read_memory",
    ]
    assert provider.finalization_tool_names(ctx) == ["append_memory_record"]

    record = MemoryRecord(
        in_channel="telegram/chat/1",
        contacts=[],
        input="hello",
        output="world",
        compacted=["step"],
    )
    append_result = provider.execute_tool(
        "append_memory_record",
        {"record": record.model_dump(mode="json")},
        ctx,
    )

    assert append_result == {"id": record.id_}
    assert provider.execute_tool("list_latest_memories", {"limit": 5}, ctx) == [
        record.id_
    ]
    assert provider.execute_tool(
        "read_memory", {"memory_id": record.id_}, ctx
    ) == record.model_dump(mode="json")
    grounding = provider.build_grounding(ctx)
    assert len(grounding) == 1
    assert grounding[0].content.startswith("<Memories>")


def test_preferences_provider_reuses_existing_prompt_loader(tmp_path: Path) -> None:
    config_base = tmp_path / ".kapybara"
    pref_root = config_base / "preferences"
    pref_root.mkdir(parents=True)
    (pref_root / "PREFERENCES.default.md").write_text("I like concise replies.\n")
    (pref_root / "telegram.md").write_text("Use Telegram formatting.\n")

    provider = PreferencesProvider.from_config(
        PreferencesProviderConfig(config_base=config_base)
    )
    ctx = TurnContext(
        in_channel="telegram/chat/1",
        out_channel=None,
        contacts=("telegram/42",),
        content="hello",
    )

    grounding = provider.build_grounding(ctx)

    assert provider.name == "preferences"
    assert len(grounding) == 1
    assert grounding[0].content.startswith("<Preferences>")
    assert provider.runtime_tool_names(ctx) == ["load_preferences"]
    assert provider.finalization_tool_names(ctx) == []
    assert provider.execute_tool("load_preferences", {}, ctx) == grounding[0].content


def test_skills_provider_reuses_existing_skill_prompt_layout(tmp_path: Path) -> None:
    config_base = tmp_path / ".kapybara"
    skills_root = config_base / "skills"
    (skills_root / "core" / "demo" / "SKILLS.md").parent.mkdir(parents=True)
    (skills_root / "core" / "demo" / "SKILLS.md").write_text("# demo\n")
    (skills_root / "messager" / "telegram" / "SKILLS.md").parent.mkdir(parents=True)
    (skills_root / "messager" / "telegram" / "SKILLS.md").write_text("# telegram\n")

    provider = SkillsProvider.from_config(SkillsProviderConfig(config_base=config_base))
    ctx = TurnContext(
        in_channel="telegram/chat/1",
        out_channel=None,
        contacts=(),
        content="hello",
    )

    grounding = provider.build_grounding(ctx)

    assert provider.name == "skills"
    assert provider.runtime_tool_names(ctx) == [
        "read_skill_document",
        "read_channel_skill_document",
    ]
    assert provider.finalization_tool_names(ctx) == []
    assert grounding[0].content.startswith("<BasicSkills>")
    assert grounding[1].content.startswith("<ChannelSkills>")
    assert provider.execute_tool(
        "read_skill_document",
        {"relative_path": "core/demo/SKILLS.md"},
        ctx,
    ) == {
        "uri": "skills:core/demo/SKILLS.md",
        "content": "# demo\n",
    }
    assert provider.execute_tool(
        "read_channel_skill_document",
        {"group": "messager"},
        ctx,
    ) == {
        "uri": "skills:messager/telegram/SKILLS.md",
        "content": "# telegram\n",
    }


def test_composite_provider_aggregates_grounding_and_namespaced_tools(
    tmp_path: Path,
) -> None:
    config_base = tmp_path / ".kapybara"
    pref_root = config_base / "preferences"
    pref_root.mkdir(parents=True)
    (pref_root / "PREFERENCES.default.md").write_text("I like concise replies.\n")
    skills_root = config_base / "skills"
    (skills_root / "core" / "demo" / "SKILLS.md").parent.mkdir(parents=True)
    (skills_root / "core" / "demo" / "SKILLS.md").write_text("# demo\n")

    composite = CompositeContextProvider(
        [
            PreferencesProvider.from_config(
                PreferencesProviderConfig(config_base=config_base)
            ),
            SkillsProvider.from_config(SkillsProviderConfig(config_base=config_base)),
        ]
    )
    ctx = TurnContext(
        in_channel="telegram/chat/1",
        out_channel=None,
        contacts=(),
        content="hello",
    )

    grounding = composite.build_grounding(ctx)

    assert len(grounding) == 2
    assert grounding[0].content.startswith("<Preferences>")
    assert grounding[1].content.startswith("<BasicSkills>")
    assert composite.runtime_tool_names(ctx) == [
        "preferences.load_preferences",
        "skills.read_skill_document",
        "skills.read_channel_skill_document",
    ]

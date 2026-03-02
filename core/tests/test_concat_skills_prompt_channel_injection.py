from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from k.agent.core.agent import concat_skills_prompt
from k.agent.core.entities import Event
from k.agent.core.run import _extract_input_event_channel_root
from k.config import Config


def _write_skill(config_base: Path, *, group: str, name: str, content: str) -> None:
    path = config_base / "skills" / group / name / "SKILLS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_extract_input_event_channel_root_from_event_json() -> None:
    event_json = Event(
        in_channel="telegram/chat/1",
        contact="telegram/1",
        content="{}",
    ).model_dump_json()
    assert _extract_input_event_channel_root([event_json]) == "telegram"


def test_extract_input_event_channel_root_ignores_non_event_strings() -> None:
    assert _extract_input_event_channel_root(["hello"]) is None


def test_concat_skills_prompt_injects_channel_root_specific_skills(
    tmp_path: Path,
) -> None:
    config_base = tmp_path / ".kapybara"
    _write_skill(config_base, group="core", name="web-search", content="core skill")
    _write_skill(
        config_base, group="meta", name="retrieve-memory", content="meta skill"
    )
    _write_skill(
        config_base, group="context", name="telegram", content="context telegram"
    )
    _write_skill(
        config_base,
        group="messager",
        name="telegram",
        content="messager telegram",
    )

    config = Config(config_base=config_base)
    event = Event(
        in_channel="telegram/chat/1",
        contact="telegram/1",
        out_channel="telegram/chat/1/thread/2",
        content="{}",
    )
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            config=config,
            start_event=event,
        )
    )

    prompt = concat_skills_prompt(ctx)  # type: ignore[arg-type]
    assert "<BasicSkills>" in prompt
    assert "<ChannelSkills>" in prompt
    assert "# ===== skills:context/telegram/SKILLS.md =====" in prompt
    assert "context telegram" in prompt
    assert "# ===== skills:messager/telegram/SKILLS.md =====" in prompt
    assert "messager telegram" in prompt


def test_concat_skills_prompt_skips_channel_skills_when_unknown_roots(
    tmp_path: Path,
) -> None:
    config_base = tmp_path / ".kapybara"
    _write_skill(config_base, group="core", name="web-search", content="core skill")
    _write_skill(
        config_base, group="meta", name="retrieve-memory", content="meta skill"
    )

    config = Config(config_base=config_base)
    event = Event(
        in_channel="nope/chat/1",
        contact="nope/1",
        out_channel="nope/chat/1",
        content="{}",
    )
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            config=config,
            start_event=event,
        )
    )

    prompt = concat_skills_prompt(ctx)  # type: ignore[arg-type]
    assert "<BasicSkills>" in prompt
    assert "<ChannelSkills>" not in prompt

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from k.agent.core.agent import concat_skills_prompt
from k.agent.core.entities import Event
from k.agent.core.run import _extract_input_event_channel_root
from k.config import Config


def _write_skill(base: Path, *, group: str, name: str, content: str) -> None:
    path = base / ".kapybara" / "skills" / group / name / "SKILLS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_extract_input_event_channel_root_from_event_json() -> None:
    event_json = Event(in_channel="telegram/chat/1", content="{}").model_dump_json()
    assert _extract_input_event_channel_root([event_json]) == "telegram"


def test_extract_input_event_channel_root_ignores_non_event_strings() -> None:
    assert _extract_input_event_channel_root(["hello"]) is None


def test_concat_skills_prompt_injects_channel_root_specific_skills(
    tmp_path: Path,
) -> None:
    _write_skill(tmp_path, group="core", name="web-search", content="core skill")
    _write_skill(tmp_path, group="meta", name="retrieve-memory", content="meta skill")
    _write_skill(tmp_path, group="context", name="telegram", content="context telegram")
    _write_skill(
        tmp_path, group="messager", name="telegram", content="messager telegram"
    )

    config = Config(fs_base=tmp_path)
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            config=config,
            input_event_in_channel="telegram/chat/1",
            input_event_out_channel="telegram/chat/1/thread/2",
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
    _write_skill(tmp_path, group="core", name="web-search", content="core skill")
    _write_skill(tmp_path, group="meta", name="retrieve-memory", content="meta skill")

    config = Config(fs_base=tmp_path)
    ctx = SimpleNamespace(
        deps=SimpleNamespace(
            config=config,
            input_event_in_channel="nope/chat/1",
            input_event_out_channel="nope/chat/1",
        )
    )

    prompt = concat_skills_prompt(ctx)  # type: ignore[arg-type]
    assert "<BasicSkills>" in prompt
    assert "<ChannelSkills>" not in prompt

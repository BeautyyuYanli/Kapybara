from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from k.agent.core.agent import persona_prompt_from_fs
from k.config import Config


def test_persona_prompt_from_fs_uses_override_when_present(tmp_path: Path) -> None:
    (tmp_path / "PERSONA.md").write_text("override persona\n", encoding="utf-8")

    config = Config(fs_base=tmp_path)
    ctx = SimpleNamespace(deps=SimpleNamespace(config=config))

    prompt = persona_prompt_from_fs(ctx)  # type: ignore[arg-type]
    assert prompt == "override persona"


def test_persona_prompt_from_fs_falls_back_when_missing(tmp_path: Path) -> None:
    config = Config(fs_base=tmp_path)
    ctx = SimpleNamespace(deps=SimpleNamespace(config=config))

    prompt = persona_prompt_from_fs(ctx)  # type: ignore[arg-type]
    assert prompt == ""


def test_persona_prompt_from_fs_falls_back_when_empty(tmp_path: Path) -> None:
    (tmp_path / "PERSONA.md").write_text("   \n", encoding="utf-8")

    config = Config(fs_base=tmp_path)
    ctx = SimpleNamespace(deps=SimpleNamespace(config=config))

    prompt = persona_prompt_from_fs(ctx)  # type: ignore[arg-type]
    assert prompt == ""

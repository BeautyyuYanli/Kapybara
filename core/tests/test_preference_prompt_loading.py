from __future__ import annotations

from pathlib import Path

import pytest

from k.agent.core.agent import _channel_preference_candidates, _load_preferences_prompt


def test_channel_preference_candidates_use_root_when_present(tmp_path: Path) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    pref_root.mkdir(parents=True)
    preferred = pref_root / "PREFERENCES.md"
    preferred.write_text("preferred", encoding="utf-8")

    candidates = _channel_preference_candidates(
        "telegram/chat/123",
        pref_root=pref_root,
    )

    assert candidates[0] == preferred
    assert pref_root / "PREFERENCES.default.md" not in candidates


def test_channel_preference_candidates_fall_back_to_default_when_root_missing(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"

    candidates = _channel_preference_candidates(
        "telegram/chat/123",
        pref_root=pref_root,
    )

    assert candidates[0] == pref_root / "PREFERENCES.default.md"


@pytest.mark.parametrize("filename", ["PREFERENCES.md", "PREFERENCES.default.md"])
def test_load_preferences_prompt_accepts_root_preference_file(
    tmp_path: Path, filename: str
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    pref_root.mkdir(parents=True)
    root_pref_path = pref_root / filename
    root_pref_path.write_text("root level preference", encoding="utf-8")

    prompt = _load_preferences_prompt(
        in_channel="telegram/chat/123",
        contact="telegram/567113516",
        pref_root=pref_root,
    )

    assert prompt.startswith("<Preferences>")
    assert f"Path: {root_pref_path}" in prompt
    assert "root level preference" in prompt


def test_load_preferences_prompt_omits_default_when_root_exists(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    pref_root.mkdir(parents=True)
    preferred = pref_root / "PREFERENCES.md"
    default = pref_root / "PREFERENCES.default.md"
    preferred.write_text("preferred root", encoding="utf-8")
    default.write_text("default root", encoding="utf-8")

    prompt = _load_preferences_prompt(
        in_channel="telegram/chat/123",
        contact="telegram/567113516",
        pref_root=pref_root,
    )

    assert f"Path: {preferred}" in prompt
    assert "preferred root" in prompt
    assert f"Path: {default}" not in prompt
    assert "default root" not in prompt


def test_load_preferences_prompt_includes_contact_preference(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    contact_pref = pref_root / "contacts" / "telegram" / "567113516.md"
    contact_pref.parent.mkdir(parents=True)
    contact_pref.write_text("contact preference", encoding="utf-8")

    prompt = _load_preferences_prompt(
        in_channel="telegram/chat/123",
        contact="telegram/567113516",
        pref_root=pref_root,
    )

    assert f"Path: {contact_pref}" in prompt
    assert "contact preference" in prompt


def test_load_preferences_prompt_skips_contact_preference_when_contact_missing(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    contact_pref = pref_root / "contacts" / "telegram" / "567113516.md"
    contact_pref.parent.mkdir(parents=True)
    contact_pref.write_text("contact preference", encoding="utf-8")

    prompt = _load_preferences_prompt(
        in_channel="telegram/chat/123",
        contact=None,
        pref_root=pref_root,
    )

    assert f"Path: {contact_pref}" not in prompt
    assert "contact preference" not in prompt

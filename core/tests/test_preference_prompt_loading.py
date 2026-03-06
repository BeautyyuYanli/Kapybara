from __future__ import annotations

import os
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


def test_channel_preference_candidates_include_effective_out_channel(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"

    candidates = _channel_preference_candidates(
        "worker/job/123",
        out_channel="telegram/chat/456/thread/7",
        pref_root=pref_root,
    )

    assert pref_root / "worker.md" in candidates
    assert pref_root / "worker/job/123.md" in candidates
    assert pref_root / "telegram.md" in candidates
    assert pref_root / "telegram/chat/456/thread/7.md" in candidates
    assert candidates.index(pref_root / "worker.md") < candidates.index(
        pref_root / "telegram.md"
    )


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
        contacts=["telegram/567113516"],
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
        contacts=["telegram/567113516"],
        pref_root=pref_root,
    )

    assert f"Path: {preferred}" in prompt
    assert "preferred root" in prompt
    assert f"Path: {default}" not in prompt
    assert "default root" not in prompt


def test_load_preferences_prompt_includes_out_channel_preference(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    out_pref = pref_root / "telegram" / "chat" / "123" / "PREFERENCES.md"
    out_pref.parent.mkdir(parents=True, exist_ok=True)
    out_pref.write_text("reply via telegram", encoding="utf-8")

    prompt = _load_preferences_prompt(
        in_channel="worker/job/1",
        contacts=[],
        pref_root=pref_root,
        out_channel="telegram/chat/123",
    )

    assert f"Path: {out_pref}" in prompt
    assert "reply via telegram" in prompt


def test_load_preferences_prompt_dedupes_overlapping_in_and_out_channel_preferences(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    pref_path = pref_root / "telegram.md"
    pref_path.parent.mkdir(parents=True, exist_ok=True)
    pref_path.write_text("telegram preference", encoding="utf-8")

    prompt = _load_preferences_prompt(
        in_channel="telegram/chat/123",
        contacts=[],
        pref_root=pref_root,
        out_channel="telegram/chat/123/thread/5",
    )

    assert prompt.count(f"Path: {pref_path}") == 1
    assert prompt.count("telegram preference") == 1


def test_load_preferences_prompt_includes_contact_preference(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    contact_pref = pref_root / "contacts" / "telegram" / "567113516.md"
    contact_pref.parent.mkdir(parents=True)
    contact_pref.write_text("contact preference", encoding="utf-8")

    prompt = _load_preferences_prompt(
        in_channel="telegram/chat/123",
        contacts=["telegram/567113516"],
        pref_root=pref_root,
    )

    assert f"Path: {contact_pref}" in prompt
    assert "contact preference" in prompt


def test_load_preferences_prompt_skips_contact_preference_when_contacts_missing(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    contact_pref = pref_root / "contacts" / "telegram" / "567113516.md"
    contact_pref.parent.mkdir(parents=True)
    contact_pref.write_text("contact preference", encoding="utf-8")

    prompt = _load_preferences_prompt(
        in_channel="telegram/chat/123",
        contacts=[],
        pref_root=pref_root,
    )

    assert f"Path: {contact_pref}" not in prompt
    assert "contact preference" not in prompt


def test_load_preferences_prompt_includes_all_contact_preferences(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    first = pref_root / "contacts" / "telegram" / "100.md"
    second = pref_root / "contacts" / "telegram" / "200.md"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("first contact preference", encoding="utf-8")
    second.write_text("second contact preference", encoding="utf-8")

    prompt = _load_preferences_prompt(
        in_channel="telegram/chat/123",
        contacts=["telegram/100", "telegram/200"],
        pref_root=pref_root,
    )

    assert f"Path: {first}" in prompt
    assert "first contact preference" in prompt
    assert f"Path: {second}" in prompt
    assert "second contact preference" in prompt


def test_load_preferences_prompt_shows_resolved_symlink_target_for_contact(
    tmp_path: Path,
) -> None:
    pref_root = tmp_path / ".kapybara" / "preferences"
    data_path = pref_root / "contacts" / "data" / "c1.md"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text("contact preference from data", encoding="utf-8")

    link_path = pref_root / "contacts" / "telegram" / "567113516.md"
    link_path.parent.mkdir(parents=True, exist_ok=True)
    relative_target = Path(os.path.relpath(data_path, start=link_path.parent))
    link_path.symlink_to(relative_target)

    prompt = _load_preferences_prompt(
        in_channel="telegram/chat/123",
        contacts=["telegram/567113516"],
        pref_root=pref_root,
    )

    assert f"Path: {link_path} -> {data_path.resolve()}" in prompt
    assert "contact preference from data" in prompt

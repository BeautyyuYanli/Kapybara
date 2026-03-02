from __future__ import annotations

import os
from pathlib import Path

from k.agent.contacts import (
    load_contacts_book,
    migrate_contacts_preferences,
    resolve_contact_unique_ids,
)


def test_resolve_contact_unique_ids_creates_and_reuses_ids(tmp_path: Path) -> None:
    config_base = tmp_path / ".kapybara"

    first = resolve_contact_unique_ids(
        config_base=config_base,
        platform_contacts=["telegram/42", "telegram/42"],
    )
    assert len(first) == 1

    second = resolve_contact_unique_ids(
        config_base=config_base,
        platform_contacts=["telegram/42", "discord/alice"],
    )
    assert second[0] == first[0]
    assert len(second) == 2

    book = load_contacts_book(config_base)
    assert book[first[0]] == ["telegram/42"]
    assert book[second[1]] == ["discord/alice"]


def test_migrate_contacts_preferences_rewrites_relative_symlinks(
    tmp_path: Path,
) -> None:
    config_base = tmp_path / ".kapybara"
    pref_path = config_base / "preferences" / "contacts" / "telegram" / "42.md"
    pref_path.parent.mkdir(parents=True, exist_ok=True)
    pref_path.write_text("telegram preference", encoding="utf-8")

    report = migrate_contacts_preferences(config_base=config_base)
    assert report.created_contact_ids == 1
    assert report.created_data_files == 1
    assert report.rewritten_symlinks == 1

    assert pref_path.is_symlink()
    link_target = Path(os.readlink(pref_path))
    assert not link_target.is_absolute()

    resolved_target = (pref_path.parent / link_target).resolve()
    assert resolved_target.read_text(encoding="utf-8") == "telegram preference"

    book = load_contacts_book(config_base)
    assert len(book) == 1
    unique_id = next(iter(book))
    assert book[unique_id] == ["telegram/42"]
    assert resolved_target == (
        config_base / "preferences" / "contacts" / "data" / f"{unique_id}.md"
    )

    second_report = migrate_contacts_preferences(config_base=config_base)
    assert second_report.created_contact_ids == 0
    assert second_report.created_data_files == 0
    assert second_report.rewritten_symlinks == 0

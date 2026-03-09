from __future__ import annotations

import json
from pathlib import Path

import pytest

from k.agent.contacts import (
    load_contacts_book,
    resolve_contact_unique_ids,
)


def test_resolve_contact_unique_ids_creates_and_reuses_ids(tmp_path: Path) -> None:
    config_base = tmp_path / ".kapybara"

    first = resolve_contact_unique_ids(
        config_base=config_base,
        platform_contacts=["telegram/42", "telegram/42"],
    )
    assert first[0] == "c1"
    assert len(first) == 1

    second = resolve_contact_unique_ids(
        config_base=config_base,
        platform_contacts=["telegram/42", "discord/alice"],
    )
    assert second[0] == first[0]
    assert second[1] == "c2"
    assert len(second) == 2

    book = load_contacts_book(config_base)
    assert book[first[0]] == ["telegram/42"]
    assert book[second[1]] == ["discord/alice"]


def test_load_contacts_book_rejects_legacy_long_id_format(
    tmp_path: Path,
) -> None:
    config_base = tmp_path / ".kapybara"
    contacts_path = config_base / "contacts.json"
    contacts_path.parent.mkdir(parents=True, exist_ok=True)
    contacts_path.write_text(
        json.dumps({"c_legacylongid123456": ["telegram/42"]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="contact id must match"):
        load_contacts_book(config_base)


def test_resolve_contact_unique_ids_rejects_invalid_existing_contact_id(
    tmp_path: Path,
) -> None:
    config_base = tmp_path / ".kapybara"
    contacts_path = config_base / "contacts.json"
    contacts_path.parent.mkdir(parents=True, exist_ok=True)
    contacts_path.write_text(
        json.dumps({"bad": ["telegram/42"]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="contact id must match"):
        resolve_contact_unique_ids(
            config_base=config_base,
            platform_contacts=["telegram/99"],
        )

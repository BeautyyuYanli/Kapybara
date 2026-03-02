"""Contact registry and preference migration helpers.

`contacts.json` stores a mapping:
`dict[unique_contact_id, list[platform_contact_id]]`.

Example:
`{"c1": ["telegram/42"], "c2": ["discord/alice"]}`

Runtime contract:
- Input events carry `contacts` as platform contact ids (`<platform>/<id>`).
- Memory records persist `contacts` as unique contact ids from this registry.
- Contact preference files live at:
  `preferences/contacts/<platform>/<platform_id>.md`
  and are migrated into relative symlinks that point to:
  `preferences/contacts/data/<unique_contact_id>.md`.
"""

from __future__ import annotations

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from k.agent.channels import validate_contact_path

type ContactsBook = dict[str, list[str]]

_CONTACTS_FILENAME = "contacts.json"
_BASE36_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"


@dataclass(slots=True)
class ContactsMigrationReport:
    """Summary returned by `migrate_contacts_preferences`."""

    created_contact_ids: int = 0
    created_data_files: int = 0
    rewritten_symlinks: int = 0


def contacts_book_path(config_base: str | Path) -> Path:
    """Return the canonical contacts registry path under `config_base`."""

    return Path(config_base) / _CONTACTS_FILENAME


def load_contacts_book(config_base: str | Path) -> ContactsBook:
    """Load `contacts.json` from `config_base`.

    Returns:
        Empty dict when the file does not exist.

    Raises:
        ValueError: When the JSON payload is not `dict[str, list[str]]` or when
            one platform contact id is mapped by multiple unique ids.
    """

    path = contacts_book_path(config_base)
    if not path.exists():
        return {}
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"Failed to read contacts book at {path}: {e}") from e
    try:
        decoded = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"Invalid JSON at {path}: {e}") from e
    return _coerce_contacts_book(decoded, source=path)


def save_contacts_book(config_base: str | Path, book: ContactsBook) -> None:
    """Persist `book` as canonical `contacts.json` under `config_base`."""

    path = contacts_book_path(config_base)
    normalized = _coerce_contacts_book(book, source=path)
    _atomic_write_text(
        path,
        json.dumps(normalized, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def resolve_contact_unique_ids(
    *,
    config_base: str | Path,
    platform_contacts: list[str],
) -> list[str]:
    """Resolve platform contact ids to unique contact ids.

    Unknown platform ids are inserted into `contacts.json` with auto-generated
    short unique ids (`c1`, `c2`, ... in base36).
    """

    path = contacts_book_path(config_base)
    normalized_contacts = _normalize_platform_contacts(platform_contacts)
    book = load_contacts_book(config_base)
    reverse = _reverse_contacts_index(book)
    existing_ids = set(book)
    created = False

    out: list[str] = []
    for platform_contact in normalized_contacts:
        unique_id = reverse.get(platform_contact)
        if unique_id is None:
            unique_id = _generate_unique_contact_id(existing_ids)
            existing_ids.add(unique_id)
            book[unique_id] = [platform_contact]
            reverse[platform_contact] = unique_id
            created = True
        out.append(unique_id)

    if created or not path.exists():
        save_contacts_book(config_base, book)

    return out


def contact_preference_path(*, pref_root: str | Path, platform_contact: str) -> Path:
    """Return the platform contact preference path for `<platform>/<id>`."""

    normalized = validate_contact_path(platform_contact, field_name="platform_contact")
    platform, platform_id = normalized.split("/", 1)
    return Path(pref_root) / "contacts" / platform / f"{platform_id}.md"


def migrate_contacts_preferences(config_base: str | Path) -> ContactsMigrationReport:
    """Migrate contact preferences to unique-id-backed data files + symlinks.

    Migration behavior:
    - Ensure `<config_base>/contacts.json` exists.
    - For each `preferences/contacts/<platform>/<platform_id>.md` file:
      1. Resolve or create the unique contact id in `contacts.json`.
      2. Ensure `preferences/contacts/data/<unique_id>.md` exists.
         - If missing, copy current file content.
      3. Rewrite `<platform>/<platform_id>.md` to a relative symlink targeting
         `../data/<unique_id>.md` (or equivalent relative path).
    """

    config_root = Path(config_base)
    contacts_root = config_root / "preferences" / "contacts"
    data_root = contacts_root / "data"

    book = load_contacts_book(config_root)
    reverse = _reverse_contacts_index(book)
    existing_ids = set(book)
    report = ContactsMigrationReport()

    for pref_path in _iter_platform_contact_preferences(contacts_root):
        rel = pref_path.relative_to(contacts_root)
        platform, filename = rel.parts
        platform_id = filename.removesuffix(".md")
        platform_contact = validate_contact_path(
            f"{platform}/{platform_id}",
            field_name=str(pref_path),
        )

        unique_id = reverse.get(platform_contact)
        if unique_id is None:
            unique_id = _generate_unique_contact_id(existing_ids)
            existing_ids.add(unique_id)
            book[unique_id] = [platform_contact]
            reverse[platform_contact] = unique_id
            report.created_contact_ids += 1

        data_path = data_root / f"{unique_id}.md"
        if not data_path.exists():
            payload = ""
            with suppress(OSError):
                payload = pref_path.read_text(encoding="utf-8")
            data_path.parent.mkdir(parents=True, exist_ok=True)
            data_path.write_text(payload, encoding="utf-8")
            report.created_data_files += 1

        target = Path(os.path.relpath(data_path, start=pref_path.parent))
        if pref_path.is_symlink():
            current_target = Path(os.readlink(pref_path))
            if current_target == target:
                continue

        pref_path.unlink(missing_ok=True)
        pref_path.parent.mkdir(parents=True, exist_ok=True)
        pref_path.symlink_to(target)
        report.rewritten_symlinks += 1

    save_contacts_book(config_root, book)
    return report


def _iter_platform_contact_preferences(contacts_root: Path) -> list[Path]:
    if not contacts_root.exists():
        return []

    out: list[Path] = []
    for path in contacts_root.rglob("*.md"):
        rel = path.relative_to(contacts_root)
        if len(rel.parts) != 2:
            continue
        if rel.parts[0] == "data":
            continue
        out.append(path)
    out.sort(key=lambda p: str(p))
    return out


def _normalize_platform_contacts(platform_contacts: list[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for idx, platform_contact in enumerate(platform_contacts):
        value = validate_contact_path(
            platform_contact, field_name=f"platform_contacts[{idx}]"
        )
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _coerce_contacts_book(payload: Any, *, source: Path) -> ContactsBook:
    if not isinstance(payload, dict):
        raise ValueError(
            f"Invalid contacts book at {source}: expected object mapping id -> list[str]"
        )

    out: ContactsBook = {}
    seen_platform_contacts: dict[str, str] = {}
    for raw_unique_id, raw_platform_contacts in payload.items():
        if not isinstance(raw_unique_id, str) or not raw_unique_id.strip():
            raise ValueError(
                f"Invalid contacts book at {source}: key must be a non-empty string"
            )
        unique_id = raw_unique_id.strip()

        if not isinstance(raw_platform_contacts, list):
            raise ValueError(
                f"Invalid contacts book at {source}: value for {unique_id!r} must be a list[str]"
            )

        normalized_contacts: list[str] = []
        seen_for_id: set[str] = set()
        for idx, platform_contact in enumerate(raw_platform_contacts):
            if not isinstance(platform_contact, str):
                raise ValueError(
                    f"Invalid contacts book at {source}: {unique_id!r}[{idx}] must be a string"
                )
            normalized = validate_contact_path(
                platform_contact,
                field_name=f"{unique_id}[{idx}]",
            )
            if normalized in seen_for_id:
                continue
            owner = seen_platform_contacts.get(normalized)
            if owner is not None and owner != unique_id:
                raise ValueError(
                    f"Invalid contacts book at {source}: {normalized!r} is mapped by both {owner!r} and {unique_id!r}"
                )
            seen_platform_contacts[normalized] = unique_id
            seen_for_id.add(normalized)
            normalized_contacts.append(normalized)
        out[unique_id] = normalized_contacts

    return out


def _reverse_contacts_index(book: ContactsBook) -> dict[str, str]:
    reverse: dict[str, str] = {}
    for unique_id, platform_contacts in book.items():
        for platform_contact in platform_contacts:
            owner = reverse.get(platform_contact)
            if owner is not None and owner != unique_id:
                raise ValueError(
                    f"Invalid contacts book: {platform_contact!r} is mapped by both {owner!r} and {unique_id!r}"
                )
            reverse[platform_contact] = unique_id
    return reverse


def _generate_unique_contact_id(existing_ids: set[str]) -> str:
    """Generate a short unique contact id using base36 counters.

    IDs are compact and model-friendly (`c1`, `c2`, ..., `cz`, `c10`, ...).
    Existing non-short ids are ignored when choosing the next counter.
    """

    next_value = 1
    for existing_id in existing_ids:
        parsed = _parse_short_contact_id(existing_id)
        if parsed is None:
            continue
        if parsed >= next_value:
            next_value = parsed + 1

    while True:
        candidate = f"c{_to_base36(next_value)}"
        if candidate not in existing_ids:
            return candidate
        next_value += 1


def _parse_short_contact_id(value: str) -> int | None:
    if len(value) < 2 or not value.startswith("c"):
        return None
    suffix = value[1:]
    if any(ch not in _BASE36_ALPHABET for ch in suffix):
        return None
    return int(suffix, 36)


def _to_base36(value: int) -> str:
    if value < 0:
        raise ValueError(f"value must be >= 0, got {value}")
    if value == 0:
        return "0"

    chars: list[str] = []
    while value > 0:
        value, remainder = divmod(value, 36)
        chars.append(_BASE36_ALPHABET[remainder])
    chars.reverse()
    return "".join(chars)


def _atomic_write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tf:
        tmp_path = Path(tf.name)
        tf.write(payload)
    tmp_path.replace(path)

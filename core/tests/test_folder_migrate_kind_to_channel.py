from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from k.agent.memory.entities import memory_record_id_from_created_at
from k.agent.memory.folder import FolderMemoryStore
from k.agent.memory.folder_migrate_kind_to_channel import (
    migrate_folder_memory_kind_to_channel,
)


def _write_record_files_with_legacy_kind(
    *,
    root: Path,
    created_at: datetime,
    kind: str = "telegram",
    detailed_input: str = "input",
) -> tuple[str, Path]:
    record_id = memory_record_id_from_created_at(created_at)
    relpath = (
        Path("records")
        / f"{created_at.year:04d}"
        / f"{created_at.month:02d}"
        / f"{created_at.day:02d}"
        / f"{created_at.hour:02d}"
        / f"{record_id}.core.json"
    )
    record_path = root / relpath
    record_path.parent.mkdir(parents=True, exist_ok=True)

    core_payload = {
        "created_at": created_at.isoformat(),
        "kind": kind,
        "id_": record_id,
        "parents": [],
        "children": [],
        "compacted": ["step"],
    }
    record_path.write_text(
        json.dumps(core_payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    detailed_path = record_path.with_name(f"{record_id}.detailed.jsonl")
    detailed_path.write_text(
        "\n".join([json.dumps(detailed_input), json.dumps("output")]) + "\n",
        encoding="utf-8",
    )

    order_entry = {
        "id": record_id,
        "created_at": created_at.isoformat(),
        "relpath": str(relpath),
    }
    (root / "order.jsonl").write_text(
        json.dumps(order_entry, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return record_id, record_path


def test_migration_dry_run_does_not_modify_files(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    created_at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    _record_id, record_path = _write_record_files_with_legacy_kind(
        root=root,
        created_at=created_at,
    )
    before = record_path.read_text(encoding="utf-8")

    report = migrate_folder_memory_kind_to_channel(root, dry_run=True)

    assert report.changed_files == 1
    assert report.unchanged_files == 0
    assert report.errors == []
    assert record_path.read_text(encoding="utf-8") == before


def test_folder_store_requires_migration_then_loads_after_apply(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    created_at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    record_id, record_path = _write_record_files_with_legacy_kind(
        root=root,
        created_at=created_at,
    )

    store_before = FolderMemoryStore(root)
    with pytest.raises(ValueError, match="in_channel"):
        store_before.get_latest()

    report = migrate_folder_memory_kind_to_channel(root, dry_run=False)
    assert report.changed_files == 1
    assert report.errors == []

    migrated = json.loads(record_path.read_text(encoding="utf-8"))
    assert "kind" not in migrated
    assert migrated["in_channel"] == "telegram"
    assert migrated["out_channel"] is None

    store_after = FolderMemoryStore(root)
    assert store_after.get_latest() == record_id
    record = store_after.get_by_id(record_id)
    assert record is not None
    assert record.in_channel == "telegram"
    assert record.out_channel is None

    rerun = migrate_folder_memory_kind_to_channel(root, dry_run=False)
    assert rerun.changed_files == 0
    assert rerun.unchanged_files == 1


def test_migration_infers_telegram_chat_id_from_detailed_input(tmp_path: Path) -> None:
    root = tmp_path / "memories"
    created_at = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    sample_update = (
        '{"update_id":846262453,"message":{"message_id":2254,'
        '"chat":{"id":567113516,"type":"private","username":"yanli_one"},'
        '"from":{"id":567113516,"username":"yanli_one"},"text":"ping"}}'
    )
    _record_id, record_path = _write_record_files_with_legacy_kind(
        root=root,
        created_at=created_at,
        kind="telegram",
        detailed_input=sample_update,
    )

    report = migrate_folder_memory_kind_to_channel(root, dry_run=False)
    assert report.errors == []

    migrated = json.loads(record_path.read_text(encoding="utf-8"))
    assert migrated["in_channel"] == "telegram/chat/567113516"
    assert migrated["out_channel"] is None

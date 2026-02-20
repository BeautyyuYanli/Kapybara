from __future__ import annotations

from datetime import datetime

import pytest

from k.agent.memory.entities import MemoryRecord
from k.agent.memory.simple import JsonlMemoryRecordStore


def _write_records(path: str, records: list[MemoryRecord]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(r.model_dump_json() + "\n")


def test_store_get_latest_and_get_by_id(tmp_path) -> None:
    path = tmp_path / "mem.jsonl"

    r1 = MemoryRecord(
        in_channel="test",
        input="i1",
        compacted=["c1"],
        output="o1",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    r2 = MemoryRecord(
        in_channel="test",
        input="i2",
        compacted=["c2"],
        output="o2",
        detailed=[],
        created_at=datetime(2026, 1, 2, 0, 0, 0),
        parents=[r1.id_],
    )

    _write_records(str(path), [r1, r2])
    store = JsonlMemoryRecordStore(path)

    assert store.get_latest() == r2.id_
    assert store.get_by_id(r1.id_) == r1
    assert store.get_by_id(str(r1.id_)) == r1
    assert store.get_by_ids({r2.id_, r1.id_}) == [r1, r2]
    with pytest.raises(ValueError, match="Invalid MemoryRecord id"):
        store.get_by_id("not-a-uuid")


def test_store_get_parents_and_children(tmp_path) -> None:
    path = tmp_path / "mem.jsonl"

    parent = MemoryRecord(
        in_channel="test",
        input="i1",
        compacted=["c1"],
        output="o1",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
        children=[],
    )
    child = MemoryRecord(
        in_channel="test",
        input="i2",
        compacted=["c2"],
        output="o2",
        detailed=[],
        created_at=datetime(2026, 1, 1, 1, 0, 0),
        parents=[parent.id_],
        children=[],
    )
    parent.children = [child.id_]

    _write_records(str(path), [parent, child])
    store = JsonlMemoryRecordStore(path)

    assert store.get_parents(child) == [parent.id_]
    assert store.get_parents(child.id_) == [parent.id_]
    assert store.get_children(parent) == [child.id_]
    assert store.get_children(parent.id_) == [child.id_]

    missing_child_id = "zzzzzzzz"
    missing = MemoryRecord(
        in_channel="test",
        input="i3",
        compacted=["c3"],
        output="o3",
        detailed=[],
        created_at=datetime(2026, 1, 1, 2, 0, 0),
        parents=[child.id_],
        children=[missing_child_id],
    )
    store.append(missing)

    # By default, missing links are skipped.
    assert store.get_children(missing) == [missing_child_id]
    assert store.get_parents(missing) == [child.id_]
    assert store.get_by_id(child.id_) is not None
    assert missing.id_ in store.get_by_id(child.id_).children  # type: ignore[union-attr]

    with pytest.raises(KeyError, match="Missing child record"):
        store.get_children(missing, strict=True)

    assert store.get_ancestors(missing) == [child.id_, parent.id_]
    assert store.get_ancestors(missing, level=0) == []
    assert store.get_ancestors(missing, level=1) == [child.id_]
    assert store.get_ancestors(missing, level=2) == [child.id_, parent.id_]


def test_store_get_between(tmp_path) -> None:
    path = tmp_path / "mem.jsonl"

    r1 = MemoryRecord(
        in_channel="test",
        input="i1",
        compacted=["c1"],
        output="o1",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    r2 = MemoryRecord(
        in_channel="test",
        input="i2",
        compacted=["c2"],
        output="o2",
        detailed=[],
        created_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    r3 = MemoryRecord(
        in_channel="test",
        input="i3",
        compacted=["c3"],
        output="o3",
        detailed=[],
        created_at=datetime(2026, 1, 2, 0, 0, 0),
    )

    _write_records(str(path), [r1, r2, r3])
    store = JsonlMemoryRecordStore(path)

    assert store.get_between(datetime(2026, 1, 1), datetime(2026, 1, 2)) == [
        r1.id_,
        r2.id_,
        r3.id_,
    ]
    assert store.get_between(
        datetime(2026, 1, 1),
        datetime(2026, 1, 2),
        include_end=False,
    ) == [r1.id_, r2.id_]


def test_store_auto_refreshes_on_external_append(tmp_path) -> None:
    path = tmp_path / "mem.jsonl"

    r1 = MemoryRecord(
        in_channel="test",
        input="i1",
        compacted=["c1"],
        output="o1",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    _write_records(str(path), [r1])

    store = JsonlMemoryRecordStore(path)
    assert store.get_latest() == r1.id_

    r2 = MemoryRecord(
        in_channel="test",
        input="i2",
        compacted=["c2"],
        output="o2",
        detailed=[],
        created_at=datetime(2026, 1, 2, 0, 0, 0),
    )
    with open(path, "a", encoding="utf-8") as f:
        f.write(r2.model_dump_json() + "\n")

    assert store.get_latest() == r2.id_

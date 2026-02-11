from __future__ import annotations

from datetime import datetime
from uuid import uuid4

import pytest

from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore


def test_folder_store_get_latest_and_get_by_id(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    r1 = MemoryRecord(
        raw_pair=("i1", "o1"),
        compacted=["c1"],
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    store.append(r1)

    r2 = MemoryRecord(
        raw_pair=("i2", "o2"),
        compacted=["c2"],
        detailed=[],
        created_at=datetime(2026, 1, 2, 0, 0, 0),
        parents=[r1.id_],
    )
    store.append(r2)

    assert store.get_latest() == r2.id_
    assert store.get_by_id(r1.id_) == r1
    assert store.get_by_id(str(r1.id_)) == r1
    assert store.get_by_ids({r2.id_, r1.id_}) == [r1, r2]
    with pytest.raises(ValueError, match="Invalid MemoryRecord id"):
        store.get_by_id("not-a-uuid")


def test_folder_store_get_parents_children_and_ancestors(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    parent = MemoryRecord(
        raw_pair=("i1", "o1"),
        compacted=["c1"],
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
        children=[],
    )
    store.append(parent)

    child = MemoryRecord(
        raw_pair=("i2", "o2"),
        compacted=["c2"],
        detailed=[],
        created_at=datetime(2026, 1, 1, 1, 0, 0),
        parents=[parent.id_],
        children=[],
    )
    store.append(child)

    assert store.get_parents(child) == [parent.id_]
    assert store.get_parents(child.id_) == [parent.id_]
    assert store.get_children(parent) == [child.id_]
    assert store.get_children(parent.id_) == [child.id_]

    missing_child_id = str(uuid4())
    missing = MemoryRecord(
        raw_pair=("i3", "o3"),
        compacted=["c3"],
        detailed=[],
        created_at=datetime(2026, 1, 1, 2, 0, 0),
        parents=[child.id_],
        children=[missing_child_id],
    )
    store.append(missing)

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


def test_folder_store_get_between(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    r1 = MemoryRecord(
        raw_pair=("i1", "o1"),
        compacted=["c1"],
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    r2 = MemoryRecord(
        raw_pair=("i2", "o2"),
        compacted=["c2"],
        detailed=[],
        created_at=datetime(2026, 1, 1, 12, 0, 0),
    )
    r3 = MemoryRecord(
        raw_pair=("i3", "o3"),
        compacted=["c3"],
        detailed=[],
        created_at=datetime(2026, 1, 2, 0, 0, 0),
    )
    store.append(r1)
    store.append(r2)
    store.append(r3)

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


def test_folder_store_auto_refreshes_on_external_append(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    r1 = MemoryRecord(
        raw_pair=("i1", "o1"),
        compacted=["c1"],
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    store.append(r1)
    assert store.get_latest() == r1.id_

    external = FolderMemoryStore(root)
    r2 = MemoryRecord(
        raw_pair=("i2", "o2"),
        compacted=["c2"],
        detailed=[],
        created_at=datetime(2026, 1, 2, 0, 0, 0),
    )
    external.append(r2)

    assert store.get_latest() == r2.id_

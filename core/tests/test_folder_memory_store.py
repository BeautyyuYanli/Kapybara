from __future__ import annotations

import json
import multiprocessing as mp
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime

import pytest

from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore


def _refresh_store_many_times(root: str, iterations: int) -> list[str]:
    store = FolderMemoryStore(root)
    for _ in range(iterations):
        store.refresh()
    return store.get_latests()


def _append_records_with_slow_core_publish(
    root: str, *, count: int, core_sleep_seconds: float
) -> list[str]:
    store = FolderMemoryStore(root)
    original_atomic_write_text = FolderMemoryStore._atomic_write_text

    def slow_atomic_write_text(self, path, text) -> None:
        original_atomic_write_text(self, path, text)
        if path.name.endswith(".core.json"):
            time.sleep(core_sleep_seconds)

    FolderMemoryStore._atomic_write_text = slow_atomic_write_text
    try:
        appended_ids: list[str] = []
        for second in range(count):
            record = MemoryRecord(
                in_channel="test",
                input=f"append-{second}",
                compacted=[f"c{second}"],
                output=f"o{second}",
                detailed=[],
                created_at=datetime(2026, 1, 2, 0, 0, second),
            )
            store.append(record)
            appended_ids.append(record.id_)
        return appended_ids
    finally:
        FolderMemoryStore._atomic_write_text = original_atomic_write_text


def test_folder_store_get_latests_and_get_by_id(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    r1 = MemoryRecord(
        in_channel="test",
        input="i1",
        compacted=["c1"],
        output="o1",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    store.append(r1)

    r1_path = root / "records" / "2026" / "01" / "01" / "00" / f"{r1.id_}.core.json"
    assert r1_path.exists()
    assert r1_path.read_text(encoding="utf-8") == r1.dump_compated()
    core_payload = json.loads(r1_path.read_text(encoding="utf-8"))
    assert core_payload["in_channel"] == "test"
    assert core_payload["out_channel"] is None
    assert core_payload["compacted"] == ["c1"]
    assert "input" not in core_payload
    assert "output" not in core_payload
    assert "detailed" not in core_payload

    r1_detailed_path = (
        root / "records" / "2026" / "01" / "01" / "00" / f"{r1.id_}.detailed.jsonl"
    )
    assert r1_detailed_path.exists()
    detailed_lines = r1_detailed_path.read_text(encoding="utf-8").splitlines()
    assert json.loads(detailed_lines[0]) == "i1"
    assert json.loads(detailed_lines[1]) == "o1"
    assert detailed_lines[2:] == []

    assert not (
        root / "records" / "2026" / "01" / "01" / "00" / f"{r1.id_}.compacted.json"
    ).exists()

    r2 = MemoryRecord(
        in_channel="test",
        input="i2",
        compacted=["c2"],
        output="o2",
        detailed=[],
        created_at=datetime(2026, 1, 2, 0, 0, 0),
        parents=[r1.id_],
    )
    store.append(r2)

    # Force a reload from disk so assertions cover the (de)serialization path.
    store.refresh()

    assert store.get_latests() == [r2.id_, r1.id_]
    expected_r1 = r1.model_copy(update={"children": [r2.id_]})
    assert store.get_by_id(r1.id_) == expected_r1
    assert store.get_by_id(str(r1.id_)) == expected_r1
    assert store.get_by_ids({r2.id_, r1.id_}) == [expected_r1, r2]
    with pytest.raises(ValueError, match="Invalid MemoryRecord id"):
        store.get_by_id("not-a-uuid")


def test_folder_store_get_parents_children_and_ancestors(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    parent = MemoryRecord(
        in_channel="test",
        input="i1",
        compacted=["c1"],
        output="o1",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
        children=[],
    )
    store.append(parent)

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
    store.append(child)

    assert store.get_parents(child) == [parent.id_]
    assert store.get_parents(child.id_) == [parent.id_]
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

    assert store.get_children(missing) == [missing_child_id]
    assert store.get_parents(missing) == [child.id_]
    assert store.get_by_id(child.id_) is not None
    assert missing.id_ in store.get_by_id(child.id_).children  # type: ignore[union-attr]

    with pytest.raises(KeyError, match="Missing child record"):
        store.get_children(missing, strict=True)

    assert store.get_ancestors(missing) == [child.id_, parent.id_]
    assert store.get_ancestors(missing, level=0) == [missing.id_]
    assert store.get_ancestors(missing, level=1) == [child.id_]
    assert store.get_ancestors(missing, level=2) == [child.id_, parent.id_]

    # After reload, dangling child links are dropped from the visible record.
    store.refresh()
    reloaded_missing = store.get_by_id(missing.id_)
    assert reloaded_missing is not None
    assert reloaded_missing.children == []
    assert store.get_children(reloaded_missing) == []
    assert store.get_children(reloaded_missing, strict=True) == []


def test_folder_store_append_ignores_missing_parent_ids(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    parent = MemoryRecord(
        in_channel="test",
        input="p-in",
        compacted=["p-c"],
        output="p-out",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    store.append(parent)

    missing_parent_id = MemoryRecord(
        in_channel="test",
        input="m-in",
        output="m-out",
    ).id_
    child = MemoryRecord(
        in_channel="test",
        input="c-in",
        compacted=["c-c"],
        output="c-out",
        detailed=[],
        created_at=datetime(2026, 1, 1, 1, 0, 0),
        parents=[parent.id_, missing_parent_id],
    )
    store.append(child)

    assert child.parents == [parent.id_]
    assert store.get_parents(child) == [parent.id_]
    assert store.get_children(parent.id_) == [child.id_]


def test_folder_store_repairs_links_when_middle_record_missing(tmp_path) -> None:
    root = tmp_path / "mem"
    writer = FolderMemoryStore(root)

    grandparent = MemoryRecord(
        in_channel="test",
        input="gp-in",
        compacted=["gp-c"],
        output="gp-out",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    writer.append(grandparent)

    parent = MemoryRecord(
        in_channel="test",
        input="p-in",
        compacted=["p-c"],
        output="p-out",
        detailed=[],
        created_at=datetime(2026, 1, 1, 1, 0, 0),
        parents=[grandparent.id_],
    )
    writer.append(parent)

    child = MemoryRecord(
        in_channel="test",
        input="c-in",
        compacted=["c-c"],
        output="c-out",
        detailed=[],
        created_at=datetime(2026, 1, 1, 2, 0, 0),
        parents=[parent.id_],
    )
    writer.append(child)

    missing_core = (
        root / "records" / "2026" / "01" / "01" / "01" / f"{parent.id_}.core.json"
    )
    missing_detailed = (
        root / "records" / "2026" / "01" / "01" / "01" / f"{parent.id_}.detailed.jsonl"
    )
    missing_core.unlink()
    missing_detailed.unlink()

    repaired = FolderMemoryStore(root)
    assert repaired.get_by_id(parent.id_) is None
    assert repaired.get_children(grandparent.id_) == [child.id_]
    assert repaired.get_parents(child.id_) == [grandparent.id_]
    assert not (root / "order.jsonl").exists()


def test_folder_store_get_between(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

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


def test_folder_store_handles_mixed_naive_and_aware_created_at(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    aware = MemoryRecord(
        in_channel="test",
        input="aware",
        compacted=[],
        output="",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
    )
    naive = MemoryRecord(
        in_channel="test",
        input="naive",
        compacted=[],
        output="",
        detailed=[],
        created_at=datetime(2026, 1, 3, 0, 0, 0),
    )
    store.append(naive)
    store.append(aware)
    store.refresh()

    assert store.get_by_ids({aware.id_, naive.id_}) == [aware, naive]
    assert store.get_between(
        datetime(2025, 12, 31, 0, 0, 0, tzinfo=UTC),
        datetime(2026, 1, 4, 0, 0, 0),
    ) == [aware.id_, naive.id_]


def test_folder_store_auto_refreshes_on_external_append(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    r1 = MemoryRecord(
        in_channel="test",
        input="i1",
        compacted=["c1"],
        output="o1",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    store.append(r1)
    assert store.get_latests() == [r1.id_]

    external = FolderMemoryStore(root)
    r2 = MemoryRecord(
        in_channel="test",
        input="i2",
        compacted=["c2"],
        output="o2",
        detailed=[],
        created_at=datetime(2026, 1, 2, 0, 0, 0),
    )
    external.append(r2)

    assert store.get_latests() == [r2.id_, r1.id_]


def test_folder_store_refresh_is_safe_across_processes(tmp_path) -> None:
    if "fork" not in mp.get_all_start_methods():
        pytest.skip("requires fork start method")

    root = tmp_path / "mem"
    store = FolderMemoryStore(root)
    older = MemoryRecord(
        in_channel="test",
        input="older",
        compacted=["c1"],
        output="o1",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    newer = MemoryRecord(
        in_channel="test",
        input="newer",
        compacted=["c2"],
        output="o2",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 1, 0),
    )
    store.append(older)
    store.append(newer)

    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=mp.get_context("fork"),
    ) as executor:
        futures = [
            executor.submit(_refresh_store_many_times, str(root), 200) for _ in range(2)
        ]
        results = [future.result() for future in futures]

    assert results == [[newer.id_, older.id_], [newer.id_, older.id_]]


def test_folder_store_refresh_is_safe_during_external_append(tmp_path) -> None:
    if "fork" not in mp.get_all_start_methods():
        pytest.skip("requires fork start method")

    root = tmp_path / "mem"
    seed_store = FolderMemoryStore(root)
    seed = MemoryRecord(
        in_channel="test",
        input="seed",
        compacted=["seed"],
        output="seed-out",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    seed_store.append(seed)

    with ProcessPoolExecutor(
        max_workers=2,
        mp_context=mp.get_context("fork"),
    ) as executor:
        refresh_future = executor.submit(_refresh_store_many_times, str(root), 200)
        append_future = executor.submit(
            _append_records_with_slow_core_publish,
            str(root),
            count=20,
            core_sleep_seconds=0.01,
        )
        appended_ids = append_future.result()
        observed_ids = refresh_future.result()

    rebuilt = FolderMemoryStore(root)
    assert rebuilt.get_by_id(appended_ids[-1]) is not None
    assert rebuilt.get_latests()[0] == appended_ids[-1]
    assert seed.id_ in observed_ids


def test_folder_store_reads_external_record_writes_directly(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    r1 = MemoryRecord(
        in_channel="test",
        input="i1",
        compacted=["c1"],
        output="o1",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    store.append(r1)

    r2 = MemoryRecord(
        in_channel="test",
        input="i2",
        compacted=["c2"],
        output="o2",
        detailed=[],
        created_at=datetime(2026, 1, 2, 0, 0, 0),
    )
    r2_core = root / "records" / "2026" / "01" / "02" / "00" / f"{r2.id_}.core.json"
    r2_detailed = (
        root / "records" / "2026" / "01" / "02" / "00" / f"{r2.id_}.detailed.jsonl"
    )
    r2_core.parent.mkdir(parents=True, exist_ok=True)
    r2_core.write_text(r2.dump_compated(), encoding="utf-8")
    r2_detailed.write_text(
        "\n".join(
            (
                json.dumps(r2.input, ensure_ascii=False),
                json.dumps(r2.output, ensure_ascii=False),
                "",
            )
        ),
        encoding="utf-8",
    )

    # Disk-backed queries should see out-of-band record files without a rebuild step.
    assert store.get_latests() == [r2.id_, r1.id_]


def test_folder_store_does_not_write_index_sidecars(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

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
    )
    store.append(r1)
    store.append(r2)

    assert not (root / "index").exists()


def test_folder_store_ignores_stale_index_sidecars(tmp_path) -> None:
    root = tmp_path / "mem"
    writer = FolderMemoryStore(root)

    record = MemoryRecord(
        in_channel="test",
        input="x",
        compacted=[],
        output="",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    writer.append(record)

    index_dir = root / "index"
    index_dir.mkdir(parents=True)
    stats_path = root / "index" / "stats.json"
    order_path = root / "index" / "order.ids"
    stats_path.write_text(
        '{"record_count":999,"latest_id":"zzzzzzzz"}\n', encoding="utf-8"
    )
    order_path.write_text("zzzzzzzz\n", encoding="utf-8")

    reader = FolderMemoryStore(root)
    assert reader.get_latests() == [record.id_]
    assert reader.get_by_id(record.id_) == record
    assert (
        stats_path.read_text(encoding="utf-8")
        == '{"record_count":999,"latest_id":"zzzzzzzz"}\n'
    )
    assert order_path.read_text(encoding="utf-8") == "zzzzzzzz\n"


def test_folder_store_order_is_lexicographic_by_id(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    high_id = MemoryRecord(
        in_channel="test",
        input="high",
        compacted=[],
        output="",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
        id_="zzzzzzzz",
    )
    low_id = MemoryRecord(
        in_channel="test",
        input="low",
        compacted=[],
        output="",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
        id_="--------",
    )

    store.append(high_id)
    store.append(low_id)
    store.refresh()

    # Latests are sorted by descending lexicographic id, not append order.
    assert store.get_latests() == [high_id.id_, low_id.id_]


def test_folder_store_scan_ignores_detailed_files(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    r1 = MemoryRecord(
        in_channel="test",
        input="i1",
        compacted=["c1"],
        output="o1",
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    store.append(r1)

    # Ensure detailed-only files are ignored when scanning for records.
    detailed_json = (
        root / "records" / "2026" / "01" / "01" / "00" / f"{r1.id_}.detailed.json"
    )
    detailed_json.write_text('{"not":"a record"}\n', encoding="utf-8")

    rebuilt = FolderMemoryStore(root)
    assert rebuilt.get_latests() == [r1.id_]
    assert rebuilt.get_by_id(r1.id_) == r1


def test_folder_store_get_latests_filters_by_in_channel_prefix(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    room = MemoryRecord(
        in_channel="telegram/chat/-1001",
        contacts=["c1"],
        input="room",
        output="",
        id_="-------0",
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    thread = MemoryRecord(
        in_channel="telegram/chat/-1001/thread/10",
        contacts=["c1", "c2"],
        input="thread",
        output="",
        id_="-------1",
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    other = MemoryRecord(
        in_channel="discord/channel/1",
        contacts=["c2"],
        input="other",
        output="",
        id_="-------2",
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    store.append(room)
    store.append(thread)
    store.append(other)

    assert store.get_latests(in_channel="telegram/chat/-1001") == [
        thread.id_,
        room.id_,
    ]
    assert store.get_latests(in_channel="telegram/chat/-1001/thread/10") == [thread.id_]
    assert store.get_latests(in_channel="telegram/chat/-1001/thread/11") == []
    assert store.get_latests(contact="c1") == [thread.id_, room.id_]
    assert store.get_latests(contact="c2") == [other.id_, thread.id_]
    assert store.get_latests(in_channel="telegram/chat/-1001", contact="c2") == [
        thread.id_
    ]
    assert store.get_latests(in_channel="telegram/chat/-1001", contact="c3") == []
    assert store.get_latests(in_channel="telegram/chat/-1001", num=1) == [thread.id_]
    assert store.get_latests(contact="c2", num=1) == [other.id_]
    assert store.get_latests(in_channel="telegram/chat/-1001", contact="c2", num=1) == [
        thread.id_
    ]
    assert store.get_latests(num=0) == []


def test_folder_store_get_latests_rejects_negative_num(tmp_path) -> None:
    store = FolderMemoryStore(tmp_path / "mem")

    with pytest.raises(ValueError, match="num must be >= 0 or None"):
        store.get_latests(num=-1)


def test_folder_store_filter_by_in_channel(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    r1 = MemoryRecord(
        in_channel="telegram/chat/-1001/thread/10",
        input="r1",
        output="",
        id_="--------",
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    r2 = MemoryRecord(
        in_channel="telegram/chat/-1001/thread/11",
        input="r2",
        output="",
        id_="-------0",
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    r3 = MemoryRecord(
        in_channel="discord/channel/1",
        input="r3",
        output="",
        id_="-------1",
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    store.append(r1)
    store.append(r2)
    store.append(r3)

    files = store.filter_by_in_channel(in_channel_prefix="telegram/chat/-1001")
    file_ids = [p.name.removesuffix(".detailed.jsonl") for p in files]

    assert file_ids == [r1.id_, r2.id_]


def test_folder_store_search_by_keywords(tmp_path) -> None:
    root = tmp_path / "mem"
    store = FolderMemoryStore(root)

    low = MemoryRecord(
        in_channel="telegram/chat/-1001/thread/10",
        input="needle-low",
        output="",
        id_="--------",
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    high = MemoryRecord(
        in_channel="telegram/chat/-1001/thread/10",
        input="x",
        output="needle-high",
        id_="zzzzzzzz",
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    store.append(low)
    store.append(high)

    files = store.filter_by_in_channel(
        in_channel_prefix="telegram/chat/-1001/thread/10"
    )
    matches = store.search_by_keywords(
        files=files,
        pattern="needle",
        n=2,
    )

    assert [p.name.removesuffix(".detailed.jsonl") for p, _ in matches] == [
        low.id_,
        high.id_,
    ]
    assert all(line_matches for _, line_matches in matches)

    latest_only = store.search_by_keywords(
        files=files,
        pattern="needle",
        n=1,
        first_match_per_file=True,
    )
    assert len(latest_only) == 1
    assert latest_only[0][0].name.removesuffix(".detailed.jsonl") == high.id_
    assert len(latest_only[0][1]) == 1

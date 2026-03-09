from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from k.agent.memory.entities import MemoryRecord, memory_record_id_from_created_at


def test_memory_record_id_is_ordered_base64_millis() -> None:
    created_at = datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert memory_record_id_from_created_at(created_at) == "--------"

    r = MemoryRecord(
        in_channel="test",
        input="i",
        compacted=["c"],
        output="o",
        detailed=[],
        created_at=created_at,
    )
    assert r.id_ == "--------"


def test_memory_record_id_lexicographic_order_matches_created_at() -> None:
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    ids = [
        memory_record_id_from_created_at(base + timedelta(milliseconds=offset))
        for offset in (0, 1, 2, 10, 11)
    ]
    assert ids == sorted(ids)
    assert {len(i) for i in ids} == {8}


def test_memory_record_id_rejects_legacy_ids() -> None:
    with pytest.raises(ValueError, match="Invalid MemoryRecord id"):
        MemoryRecord(
            in_channel="test",
            id_="019c52f782ec",
            input="i",
            compacted=["c"],
            output="o",
            detailed=[],
            created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )

    with pytest.raises(ValueError, match="Invalid MemoryRecord id"):
        MemoryRecord(
            in_channel="test",
            id_="00000000-0000-0000-0000-000000000000",
            input="i",
            compacted=["c"],
            output="o",
            detailed=[],
            created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC),
        )


def test_memory_record_id_rejects_invalid_ids() -> None:
    with pytest.raises(ValueError, match="Invalid MemoryRecord id"):
        MemoryRecord(
            in_channel="test",
            id_="not-a-uuid",
            input="i",
            compacted=["c"],
            output="o",
            detailed=[],
            created_at=datetime(2026, 1, 1, 0, 0, 0),
        )

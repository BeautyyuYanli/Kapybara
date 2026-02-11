from __future__ import annotations

from datetime import UTC, datetime

import pytest

from k.agent.memory.entities import MemoryRecord, memory_record_id_from_created_at


def test_memory_record_id_is_base64url_millis() -> None:
    created_at = datetime(1970, 1, 1, 0, 0, 0, tzinfo=UTC)
    assert memory_record_id_from_created_at(created_at) == "AAAAAAAA"

    r = MemoryRecord(
        raw_pair=("i", "o"),
        compacted=["c"],
        detailed=[],
        created_at=created_at,
    )
    assert r.id_ == "AAAAAAAA"


def test_memory_record_id_accepts_legacy_uuid() -> None:
    legacy = "00000000-0000-0000-0000-000000000000"
    r = MemoryRecord(
        id_=legacy,
        raw_pair=("i", "o"),
        compacted=["c"],
        detailed=[],
        created_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    assert r.id_ == legacy


def test_memory_record_id_rejects_invalid_ids() -> None:
    with pytest.raises(ValueError, match="Invalid MemoryRecord id"):
        MemoryRecord(
            id_="not-a-uuid",
            raw_pair=("i", "o"),
            compacted=["c"],
            detailed=[],
            created_at=datetime(2026, 1, 1, 0, 0, 0),
        )

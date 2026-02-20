from __future__ import annotations

from datetime import UTC, datetime

from k.agent.memory.entities import MemoryRecord


def test_memory_record_dump_order_is_input_compacted_output() -> None:
    r = MemoryRecord(
        in_channel="test",
        created_at=datetime(2026, 2, 13, 2, 8, 10, tzinfo=UTC),
        id_="--------",
        parents=[],
        children=[],
        input="in",
        compacted=["c1", "c2"],
        output="out",
        detailed=[],
    )

    dumped = r.model_dump_json(exclude={"detailed"})
    assert (
        dumped.index('"input"') < dumped.index('"compacted"') < dumped.index('"output"')
    )

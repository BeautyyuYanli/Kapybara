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


def test_memory_record_dump_compated_matches_core_payload() -> None:
    r = MemoryRecord(
        in_channel="test",
        created_at=datetime(2026, 2, 13, 2, 8, 10, tzinfo=UTC),
        id_="--------",
        contacts=["c1"],
        parents=[],
        children=[],
        input="in",
        compacted=["<input>in</input>", "<output>out</output>"],
        output="out",
        detailed=[],
    )

    assert r.dump_compated() == (
        '{"created_at":"2026-02-13T02:08:10Z","in_channel":"test","out_channel":null,'
        '"contacts":["c1"],"id_":"--------","parents":[],"children":[],'
        '"compacted":["<input>in</input>","<output>out</output>"]}'
    )


def test_memory_record_dump_raw_pair_uses_first_and_last_compacted_items() -> None:
    r = MemoryRecord(
        in_channel="test",
        created_at=datetime(2026, 2, 13, 2, 8, 10, tzinfo=UTC),
        id_="--------",
        parents=[],
        children=[],
        input="in",
        compacted=["<input>in</input>", "step", "<output>out</output>"],
        output="out",
        detailed=[],
    )

    assert r.dump_raw_pair() == (
        '<Record><Meta>{"id_":"--------","parents":[],"children":[]}</Meta>'
        "<input>in</input>\n<output>out</output></Record>"
    )


def test_memory_record_dump_raw_pair_handles_empty_and_single_compacted() -> None:
    empty = MemoryRecord(
        in_channel="test",
        created_at=datetime(2026, 2, 13, 2, 8, 10, tzinfo=UTC),
        id_="--------",
        parents=[],
        children=[],
        input="in",
        compacted=[],
        output="out",
        detailed=[],
    )
    single = MemoryRecord(
        in_channel="test",
        created_at=datetime(2026, 2, 13, 2, 8, 10, tzinfo=UTC),
        id_="-------0",
        parents=[],
        children=[],
        input="in",
        compacted=["only"],
        output="out",
        detailed=[],
    )

    assert empty.dump_raw_pair() == (
        '<Record><Meta>{"id_":"--------","parents":[],"children":[]}</Meta></Record>'
    )
    assert single.dump_raw_pair() == (
        '<Record><Meta>{"id_":"-------0","parents":[],"children":[]}</Meta>only</Record>'
    )

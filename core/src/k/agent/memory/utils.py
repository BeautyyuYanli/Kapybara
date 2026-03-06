"""Shared helpers for memory-id selection.

These helpers keep the channel-scoped and contact-scoped retrieval modules in
sync on deduplication, ancestor expansion, and cap handling. Callers that need
full `MemoryRecord` payloads should resolve the returned ids through
`MemoryStore.get_by_ids()`.
"""

from __future__ import annotations

from collections.abc import Sequence

from k.agent.memory.entities import MemoryRecord
from k.agent.memory.store import MemoryStore


def dedupe_memory_ids(ids: Sequence[str]) -> list[str]:
    """Return ids in first-seen order with duplicates removed."""

    out: list[str] = []
    seen: set[str] = set()
    for id_ in ids:
        if id_ in seen:
            continue
        seen.add(id_)
        out.append(id_)
    return out


def keep_latest_records(
    records: list[MemoryRecord],
    *,
    cap_num: int,
    name: str,
) -> list[MemoryRecord]:
    """Return the latest `cap_num` records from a datetime-sorted list."""

    if cap_num < 0:
        raise ValueError(f"{name} must be >= 0; got {cap_num}")
    if cap_num == 0:
        return []
    return records[-cap_num:]


def select_memory_ids_from_roots(
    memory_store: MemoryStore,
    *,
    roots: Sequence[str],
    compacted_level_num: int,
    raw_pair_level_num: int,
    compacted_cap_num: int,
    raw_pair_cap_num: int,
    cap_name_prefix: str,
) -> tuple[list[str], list[str]]:
    """Return compacted/raw-pair ids for one retrieval scope.

    Contract:
    - `roots` are included in both traversal sets before ancestor expansion.
    - compacted overflow is downgraded into raw-pair candidates before the
      raw-pair cap is applied.
    - returned ids are chronologically sorted via `MemoryStore.get_by_ids()`.
    - returned raw-pair ids are disjoint from returned compacted ids.
    """

    deduped_roots = dedupe_memory_ids(roots)
    compacted_mem = set(deduped_roots)
    raw_mem = set(deduped_roots)

    for root_id in deduped_roots:
        compacted_mem.update(
            memory_store.get_ancestors(root_id, level=compacted_level_num)
        )
        raw_mem.update(memory_store.get_ancestors(root_id, level=raw_pair_level_num))

    raw_pair_only = raw_mem - compacted_mem
    compacted_source_records = memory_store.get_by_ids(compacted_mem)
    compacted_records = keep_latest_records(
        compacted_source_records,
        cap_num=compacted_cap_num,
        name=f"{cap_name_prefix}_compacted_cap_num",
    )
    compacted_ids = [record.id_ for record in compacted_records]
    downgraded_ids = {record.id_ for record in compacted_source_records} - set(
        compacted_ids
    )
    raw_pair_candidates = raw_pair_only | downgraded_ids
    raw_pair_records = keep_latest_records(
        memory_store.get_by_ids(raw_pair_candidates),
        cap_num=raw_pair_cap_num,
        name=f"{cap_name_prefix}_raw_pair_cap_num",
    )
    raw_pair_ids = [record.id_ for record in raw_pair_records]
    return compacted_ids, raw_pair_ids

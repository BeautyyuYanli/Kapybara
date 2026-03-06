"""Shared helpers for memory-id selection.

These helpers keep the channel-scoped, contact-scoped, and caller-supplied
root-id retrieval paths in sync on deduplication, ancestor expansion, cap
handling, and final id ordering. They intentionally stop at ids so call sites
can merge scopes first and do one final `MemoryStore.get_by_ids()` lookup.
"""

from __future__ import annotations

from collections.abc import Sequence

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


def keep_latest_ids(
    ids: Sequence[str],
    *,
    cap_num: int,
    name: str,
) -> list[str]:
    """Return the latest `cap_num` ids from a lexicographically time-sorted list."""

    if cap_num < 0:
        raise ValueError(f"{name} must be >= 0; got {cap_num}")
    if cap_num == 0:
        return []
    return list(ids[-cap_num:])


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
    - returned ids are lexicographically sorted, which matches chronological
      order for `MemoryRecord.id_`.
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
    compacted_source_ids = sorted(compacted_mem)
    compacted_ids = keep_latest_ids(
        compacted_source_ids,
        cap_num=compacted_cap_num,
        name=f"{cap_name_prefix}_compacted_cap_num",
    )
    downgraded_ids = set(compacted_source_ids) - set(compacted_ids)
    raw_pair_candidates = raw_pair_only | downgraded_ids
    raw_pair_ids = keep_latest_ids(
        sorted(raw_pair_candidates),
        cap_num=raw_pair_cap_num,
        name=f"{cap_name_prefix}_raw_pair_cap_num",
    )
    return compacted_ids, raw_pair_ids


def get_memory_ids_from_roots(
    memory_store: MemoryStore,
    *,
    roots: Sequence[str],
    compacted_level_num: int = 0,
    raw_pair_level_num: int = 3,
    compacted_cap_num: int = 15,
    raw_pair_cap_num: int = 15,
    cap_name_prefix: str = "root",
) -> tuple[list[str], set[str], list[str]]:
    """Resolve one root set into prompt-ready ids.

    Defaults keep caller-supplied roots compacted at the root itself while
    still expanding older context through the raw-pair path. That makes
    explicit parent ids additive context instead of re-promoting their
    ancestors into compacted form by default.

    Returns:
        `(selected_ids, compacted_ids, deduped_roots)`, where `selected_ids`
        are lexicographically time-sorted and `compacted_ids` marks which ids
        should be rendered with `MemoryRecord.dump_compated()`.
    """

    deduped_roots = dedupe_memory_ids(roots)
    compacted_ids, raw_pair_ids = select_memory_ids_from_roots(
        memory_store,
        roots=deduped_roots,
        compacted_level_num=compacted_level_num,
        raw_pair_level_num=raw_pair_level_num,
        compacted_cap_num=compacted_cap_num,
        raw_pair_cap_num=raw_pair_cap_num,
        cap_name_prefix=cap_name_prefix,
    )
    selected_ids = sorted(set(compacted_ids) | set(raw_pair_ids))
    return selected_ids, set(compacted_ids), deduped_roots

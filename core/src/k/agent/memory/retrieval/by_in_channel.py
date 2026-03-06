"""Channel-scoped memory retrieval helpers.

This module implements the auto-injection policy for `Event.in_channel`.
Selection happens in two phases:
1. Resolve latest root memories from one `in_channel` subtree.
2. Expand those roots into compacted/raw-pair ids using the shared downgrade
   and cap rules from `k.agent.memory.utils.select_memory_ids_from_roots`.

The underlying store treats the `in_channel` filter as a routing-subtree
match: either `MemoryRecord.in_channel` or the record's effective output
channel may satisfy the prefix.
"""

from __future__ import annotations

from collections.abc import Sequence

from k.agent.memory.store import MemoryStore
from k.agent.memory.utils import dedupe_memory_ids, select_memory_ids_from_roots


def latest_memory_roots_by_in_channel(
    memory_store: MemoryStore,
    *,
    in_channel: str,
    latest_num: int = 5,
) -> list[str]:
    """Return latest root ids for the routed `in_channel` subtree."""

    if latest_num < 0:
        raise ValueError(f"latest_num must be >= 0; got {latest_num}")
    if latest_num == 0:
        return []
    return dedupe_memory_ids(
        memory_store.get_latests(
            in_channel=in_channel,
            num=latest_num,
        )
    )


def select_memory_ids_by_in_channel(
    memory_store: MemoryStore,
    *,
    in_channel: str,
    roots: Sequence[str] | None = None,
    latest_num: int = 5,
    compacted_level_num: int = 1,
    raw_pair_level_num: int = 3,
    compacted_cap_num: int = 15,
    raw_pair_cap_num: int = 15,
) -> tuple[list[str], list[str]]:
    """Return `(compacted_ids, raw_pair_ids)` for one channel subtree.

    When `roots` is provided, the caller owns root selection and `latest_num`
    is ignored. Returned raw-pair ids exclude the ids kept in compacted form.
    """

    resolved_roots = (
        dedupe_memory_ids(roots)
        if roots is not None
        else latest_memory_roots_by_in_channel(
            memory_store,
            in_channel=in_channel,
            latest_num=latest_num,
        )
    )
    return select_memory_ids_from_roots(
        memory_store,
        roots=resolved_roots,
        compacted_level_num=compacted_level_num,
        raw_pair_level_num=raw_pair_level_num,
        compacted_cap_num=compacted_cap_num,
        raw_pair_cap_num=raw_pair_cap_num,
        cap_name_prefix="channel",
    )

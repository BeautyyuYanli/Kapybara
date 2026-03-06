"""Contact-scoped memory retrieval helpers.

Each contact contributes its own latest roots. The merged root list is
deduplicated in contact iteration order before ancestor expansion so contact
priority stays explicit at the call site.
"""

from __future__ import annotations

from collections.abc import Sequence

from k.agent.memory.store import MemoryStore
from k.agent.memory.utils import dedupe_memory_ids, select_memory_ids_from_roots


def latest_memory_roots_by_contact(
    memory_store: MemoryStore,
    *,
    contacts: Sequence[str],
    latest_num: int = 1,
) -> list[str]:
    """Return latest root ids aggregated across the given contacts."""

    if latest_num < 0:
        raise ValueError(f"latest_num must be >= 0; got {latest_num}")
    if latest_num == 0:
        return []

    roots: list[str] = []
    for contact in contacts:
        roots.extend(
            memory_store.get_latests(
                contact=contact,
                num=latest_num,
            )
        )
    return dedupe_memory_ids(roots)


def select_memory_ids_by_contact(
    memory_store: MemoryStore,
    *,
    contacts: Sequence[str],
    roots: Sequence[str] | None = None,
    latest_num: int = 1,
    compacted_level_num: int = 0,
    raw_pair_level_num: int = 1,
    compacted_cap_num: int = 5,
    raw_pair_cap_num: int = 5,
) -> tuple[list[str], list[str]]:
    """Return `(compacted_ids, raw_pair_ids)` for the given contacts.

    When `roots` is provided, the caller owns root selection and `latest_num`
    is ignored. Returned raw-pair ids exclude the ids kept in compacted form.
    """

    resolved_roots = (
        dedupe_memory_ids(roots)
        if roots is not None
        else latest_memory_roots_by_contact(
            memory_store,
            contacts=contacts,
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
        cap_name_prefix="contact",
    )

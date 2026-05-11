"""Shared protocol for :class:`k.agent.memory.entities.MemoryRecord` stores.

This module defines the structural interface used by agents to query and append
`MemoryRecord` objects while preserving the parent/child relationship graph.

Design notes / invariants:
- A store is the source of truth for which records exist. Links stored on a
  record (`parents` / `children`) may refer to missing records; link-resolution
  methods support `strict` mode to surface this.
- `contains_id()` is the cheap existence probe for orchestration code that only
  needs to know whether a record is present yet. Implementations should avoid
  full record loads when possible, while preserving the same visible existence
  semantics as `get_by_id(id_) is not None`.
- "Latests" are returned in descending store order (newest first) and can be
  filtered by routing subtree via `in_channel` and exact contact-id membership
  via `contact`, with optional result capping via `num`. A routing match
  succeeds when either `record.in_channel` or its effective output channel is
  in the requested subtree.
- `append()` must treat `record.parents` as the source of truth and ensure each
  referenced parent record contains `record.id_` in its `children` list before
  returning.
"""

from __future__ import annotations

from collections.abc import Set
from datetime import datetime
from typing import Protocol, runtime_checkable

from k.agent.memory.entities import MemoryRecord, is_memory_record_id

type MemoryRecordId = str
type MemoryRecordRef = MemoryRecord | str


def coerce_record_id(value: MemoryRecordId) -> str:
    """Coerce a record id into its canonical string form.

    Raises:
        ValueError: If `value` is not a valid MemoryRecord id.
    """

    if not is_memory_record_id(value):
        raise ValueError(f"Invalid MemoryRecord id: {value!r}")
    return value


@runtime_checkable
class MemoryStore(Protocol):
    """Protocol for persisting and querying `MemoryRecord` objects."""

    def refresh(self) -> None:
        """Force a reload from disk (even if the underlying storage did not change)."""
        ...

    def contains_id(self, id_: MemoryRecordId) -> bool:
        """Return whether `id_` exists, preferring metadata-only checks when possible."""
        ...

    def get_latests(
        self,
        *,
        in_channel: str | None = None,
        contact: str | None = None,
        num: int | None = None,
    ) -> list[str]:
        """Return latest record ids (descending store order).

        Args:
            in_channel: Optional routing-channel prefix filter. A record is
                selected when either its `in_channel` or its effective output
                channel equals this prefix or starts with `"{prefix}/"`.
            contact: Optional contact filter. A record is selected when this
                contact id exists in `MemoryRecord.contacts`.
            num: Optional maximum number of ids to return. `None` means no
                limit.
        """
        ...

    def get_by_id(self, id_: MemoryRecordId) -> MemoryRecord | None:
        """Return a record by id, or `None` if missing."""
        ...

    def get_by_ids(
        self, ids: Set[MemoryRecordId], *, strict: bool = False
    ) -> list[MemoryRecord]:
        """Return records for `ids`, sorted by (`created_at`, store order)."""
        ...

    def get_parents(
        self, record: MemoryRecordRef, *, strict: bool = False
    ) -> list[str]:
        """Return parent ids for `record` (in the same order as `record.parents`)."""
        ...

    def get_children(
        self, record: MemoryRecordRef, *, strict: bool = False
    ) -> list[str]:
        """Return child ids for `record` (in the same order as `record.children`)."""
        ...

    def get_ancestors(
        self,
        record: MemoryRecordRef,
        *,
        level: int | None = None,
        strict: bool = False,
    ) -> list[str]:
        """Return ancestor ids for `record` by repeatedly following parents.

        Level semantics:
        - `level=0`: return a single-item list with `record` itself.
        - `level=1`: return direct parent ids.
        - `level=None`: return all reachable ancestors.
        """
        ...

    def get_between(
        self,
        start: datetime,
        end: datetime,
        *,
        include_start: bool = True,
        include_end: bool = True,
    ) -> list[str]:
        """Return record ids whose `created_at` falls within the given range."""
        ...

    def append(self, record: MemoryRecord) -> None:
        """Persist `record` and update parents' `children` links."""
        ...

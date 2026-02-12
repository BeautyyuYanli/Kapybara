"""Shared protocol for :class:`k.agent.memory.entities.MemoryRecord` stores.

This module defines the structural interface used by agents to query and append
`MemoryRecord` objects while preserving the parent/child relationship graph.

Design notes / invariants:
- A store is the source of truth for which records exist. Links stored on a
  record (`parents` / `children`) may refer to missing records; link-resolution
  methods support `strict` mode to surface this.
- "Latest" is defined as the most recently appended record id (i.e. the tail of
  the store's append order), not necessarily the max `created_at`.
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

    def get_latest(self) -> str | None:
        """Return the latest record id (store append order), or `None` if empty."""

    def get_by_id(self, id_: MemoryRecordId) -> MemoryRecord | None:
        """Return a record by id, or `None` if missing."""

    def get_by_ids(
        self, ids: Set[MemoryRecordId], *, strict: bool = False
    ) -> list[MemoryRecord]:
        """Return records for `ids`, sorted by (`created_at`, store order)."""

    def get_parents(
        self, record: MemoryRecordRef, *, strict: bool = False
    ) -> list[str]:
        """Return parent ids for `record` (in the same order as `record.parents`)."""

    def get_children(
        self, record: MemoryRecordRef, *, strict: bool = False
    ) -> list[str]:
        """Return child ids for `record` (in the same order as `record.children`)."""

    def get_ancestors(
        self,
        record: MemoryRecordRef,
        *,
        level: int | None = None,
        strict: bool = False,
    ) -> list[str]:
        """Return ancestor ids for `record` by repeatedly following parents."""

    def get_between(
        self,
        start: datetime,
        end: datetime,
        *,
        include_start: bool = True,
        include_end: bool = True,
    ) -> list[str]:
        """Return record ids whose `created_at` falls within the given range."""

    def append(self, record: MemoryRecord) -> None:
        """Persist `record` and update parents' `children` links."""

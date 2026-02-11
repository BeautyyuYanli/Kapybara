"""Simple, JSONL-backed storage for :class:`k.agent.memory.entities.MemoryRecord`.

This module provides a small helper class for reading (and optionally appending)
`MemoryRecord` entries stored as JSON Lines (one JSON object per non-empty line).

Design notes / invariants:
- "Latest" means the last record in file order, not necessarily the max
  `created_at`.
- Parsing is strict: invalid JSON or invalid `MemoryRecord` data raises a
  `ValueError` with line context, since silently skipping corrupt lines can hide
  data-loss bugs.
- The store caches parsed records and auto-refreshes when the underlying file's
  mtime/size changes.
- When appending a new record, this store updates each referenced parent's
  `children` list and persists those changes (by rewriting the JSONL file as a
  whole). This keeps parent/child links consistent when new records are created
  with empty `children`.
"""

from __future__ import annotations

import tempfile
from collections.abc import Set
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from k.agent.memory.entities import MemoryRecord
from k.agent.memory.store import (
    MemoryRecordId,
    MemoryRecordRef,
    MemoryStore,
    coerce_record_id,
)


@dataclass(slots=True)
class _CacheKey:
    mtime_ns: int
    size: int


class JsonlMemoryRecordStore(MemoryStore):
    """Query `MemoryRecord` objects stored in a JSONL file.

    Args:
        path: Path to the JSONL file.
        encoding: File encoding used for reading/writing.

    The store is safe to use as a read-mostly helper in a single process.
    If multiple writers append concurrently, readers may observe partial writes;
    in that scenario, prefer an append strategy that writes whole lines atomically.
    """

    path: Path
    encoding: str

    _cache_key: _CacheKey | None
    _records: list[MemoryRecord]
    _by_id: dict[str, MemoryRecord]

    def __init__(self, path: str | Path, *, encoding: str = "utf-8") -> None:
        self.path = Path(path)
        self.encoding = encoding
        self._cache_key = None
        self._records = []
        self._by_id = {}

    def refresh(self) -> None:
        """Force a reload from disk (even if the file did not change)."""

        self._cache_key = None
        self._load_if_needed()

    def get_latest(self) -> str | None:
        """Return the latest record id (last line in file order), or `None` if empty."""

        self._load_if_needed()
        return self._records[-1].id_ if self._records else None

    def get_by_id(self, id_: MemoryRecordId) -> MemoryRecord | None:
        """Return a record by id, or `None` if missing."""

        self._load_if_needed()
        record_id = coerce_record_id(id_)
        return self._by_id.get(record_id)

    def get_by_ids(
        self, ids: Set[MemoryRecordId], *, strict: bool = False
    ) -> list[MemoryRecord]:
        """Return records for ids, sorted by `created_at` (then file order).

        Args:
            ids: Record ids. Duplicates are ignored.
            strict: If true, raise `KeyError` if any id is missing.
        """

        self._load_if_needed()

        record_ids = {coerce_record_id(id_) for id_ in ids}
        missing = [id_ for id_ in record_ids if id_ not in self._by_id]
        if strict and missing:
            missing_str = ", ".join(str(i) for i in sorted(missing))
            raise KeyError(f"Missing record(s): {missing_str}")

        records = [self._by_id[id_] for id_ in record_ids if id_ in self._by_id]
        order = {record.id_: idx for idx, record in enumerate(self._records)}
        records.sort(key=lambda r: (r.created_at, order.get(r.id_, 1_000_000_000)))
        return records

    def get_parents(
        self, record: MemoryRecordRef, *, strict: bool = False
    ) -> list[str]:
        """Return parent ids for `record` (in the same order as `record.parents`).

        Args:
            record: A record object or its id.
            strict: If true, raise `KeyError` if any referenced parent id is missing.
        """

        self._load_if_needed()
        rec = self._coerce_record(record)
        if strict:
            missing = [id_ for id_ in rec.parents if id_ not in self._by_id]
            if missing:
                missing_str = ", ".join(str(i) for i in missing)
                raise KeyError(f"Missing parent record(s): {missing_str}")
        return list(rec.parents)

    def get_children(
        self, record: MemoryRecordRef, *, strict: bool = False
    ) -> list[str]:
        """Return child ids for `record` (in the same order as `record.children`).

        Args:
            record: A record object or its id.
            strict: If true, raise `KeyError` if any referenced child id is missing.
        """

        self._load_if_needed()
        rec = self._coerce_record(record)
        if strict:
            missing = [id_ for id_ in rec.children if id_ not in self._by_id]
            if missing:
                missing_str = ", ".join(str(i) for i in missing)
                raise KeyError(f"Missing child record(s): {missing_str}")
        return list(rec.children)

    def get_ancestors(
        self,
        record: MemoryRecordRef,
        *,
        level: int | None = None,
        strict: bool = False,
    ) -> list[str]:
        """Return ancestor ids for `record` by repeatedly following `get_parents`.

        Args:
            record: A record object or its id.
            level: Max ancestor depth to return. `1` returns only the immediate
                parent, `2` returns parent and grandparent, etc. `0` returns an
                empty list. `None` means no limit.
            strict: If true, raise if the chain references missing records.

        Returns:
            A de-duplicated list ordered by level: parents first, then grandparents,
            etc. For each record, parent ids are expanded in `record.parents` order.

        Notes:
            This returns ids even if some ancestor records are missing on disk
            (`strict=False`). Missing records simply stop expansion on that branch.
        """

        if level is not None and level < 0:
            raise ValueError(f"level must be >= 0 or None; got {level}")

        self._load_if_needed()
        current = self._coerce_record(record)

        if level == 0:
            return []

        ancestors: list[str] = []
        seen: set[str] = set()

        frontier = self.get_parents(current, strict=strict)
        depth = 0
        while frontier and (level is None or depth < level):
            depth += 1
            next_frontier: list[str] = []
            for parent_id in frontier:
                if parent_id in seen:
                    continue
                seen.add(parent_id)
                ancestors.append(parent_id)

                parent_record = self._by_id.get(parent_id)
                if parent_record is None:
                    if strict:
                        raise KeyError(f"Unknown parent MemoryRecord id: {parent_id}")
                    continue
                next_frontier.extend(self.get_parents(parent_record, strict=strict))
            frontier = next_frontier

        return ancestors

    def get_between(
        self,
        start: datetime,
        end: datetime,
        *,
        include_start: bool = True,
        include_end: bool = True,
    ) -> list[str]:
        """Return record ids whose `created_at` falls within `[start, end]` by default.

        Args:
            start: Range start.
            end: Range end.
            include_start: Whether `created_at == start` is included.
            include_end: Whether `created_at == end` is included.

        Returns:
            A list sorted by (`created_at`, file order).
        """

        if start > end:
            raise ValueError(f"start must be <= end; got start={start!r}, end={end!r}")

        self._load_if_needed()

        indexed: list[tuple[int, str, datetime]] = []
        for idx, record in enumerate(self._records):
            if _in_datetime_range(
                record.created_at,
                start,
                end,
                include_start=include_start,
                include_end=include_end,
            ):
                indexed.append((idx, record.id_, record.created_at))

        indexed.sort(key=lambda t: (t[2], t[0]))
        return [record_id for _, record_id, _ in indexed]

    def append(self, record: MemoryRecord) -> None:
        """Append a record and update its parents' `children` lists.

        This method treats `record.parents` as the source of truth and ensures
        each parent record has `record.id_` present in its `children` list.

        The file is rewritten to persist parent updates and to keep ids unique.
        """

        self._load_if_needed()

        if record.id_ in self._by_id:
            raise ValueError(
                f"Duplicate MemoryRecord id encountered while appending: {record.id_}"
            )

        for parent_id in record.parents:
            parent = self._by_id.get(parent_id)
            if parent is None:
                raise KeyError(f"Unknown parent MemoryRecord id: {parent_id}")
            if record.id_ not in parent.children:
                parent.children.append(record.id_)

        self._records.append(record)
        self._by_id[record.id_] = record
        self._persist_snapshot()
        self._cache_key = self._stat_key()

    def _load_if_needed(self) -> None:
        key = self._stat_key()
        if key is None:
            # Missing file counts as an empty log.
            self._cache_key = None
            self._records = []
            self._by_id = {}
            return

        if self._cache_key is not None and key == self._cache_key:
            return

        records, by_id = _read_jsonl_memory_records(self.path, encoding=self.encoding)
        self._records = records
        self._by_id = by_id
        self._cache_key = key

    def _stat_key(self) -> _CacheKey | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return _CacheKey(mtime_ns=stat.st_mtime_ns, size=stat.st_size)

    def _persist_snapshot(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = self.path.parent if self.path.parent.exists() else None
        with tempfile.NamedTemporaryFile(
            "w",
            encoding=self.encoding,
            dir=tmp_dir,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tf:
            tmp_path = Path(tf.name)
            for record in self._records:
                tf.write(record.model_dump_json() + "\n")

        tmp_path.replace(self.path)

    def _coerce_record(self, record: MemoryRecordRef) -> MemoryRecord:
        if isinstance(record, MemoryRecord):
            return record
        record_id = coerce_record_id(record)
        rec = self._by_id.get(record_id)
        if rec is None:
            raise KeyError(f"Unknown MemoryRecord id: {record_id}")
        return rec

    def _resolve_links(
        self, ids: list[str], *, link_name: str, strict: bool
    ) -> list[MemoryRecord]:
        res: list[MemoryRecord] = []
        missing: list[str] = []
        for id_ in ids:
            rec = self._by_id.get(id_)
            if rec is None:
                if strict:
                    missing.append(id_)
                continue
            res.append(rec)

        if missing:
            missing_str = ", ".join(str(i) for i in missing)
            raise KeyError(f"Missing {link_name} record(s): {missing_str}")
        return res


def _read_jsonl_memory_records(
    path: Path, *, encoding: str
) -> tuple[list[MemoryRecord], dict[str, MemoryRecord]]:
    records: list[MemoryRecord] = []
    by_id: dict[str, MemoryRecord] = {}

    with path.open("r", encoding=encoding) as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = MemoryRecord.model_validate_json(line)
            except ValidationError as e:
                raise ValueError(
                    f"Invalid MemoryRecord JSON at {path}:{line_no}: {e}"
                ) from e
            except ValueError as e:
                # JSON decode errors land here.
                raise ValueError(f"Invalid JSON at {path}:{line_no}: {e}") from e

            if record.id_ in by_id:
                raise ValueError(
                    f"Duplicate MemoryRecord id at {path}:{line_no}: {record.id_}"
                )
            records.append(record)
            by_id[record.id_] = record

    return records, by_id


def _in_datetime_range(
    value: datetime,
    start: datetime,
    end: datetime,
    *,
    include_start: bool,
    include_end: bool,
) -> bool:
    try:
        if include_start:
            left_ok = value >= start
        else:
            left_ok = value > start
        if include_end:
            right_ok = value <= end
        else:
            right_ok = value < end
        return left_ok and right_ok
    except TypeError as e:
        # Most commonly: comparing naive with aware datetimes.
        raise ValueError(
            "Datetime comparison failed. Ensure `created_at`, `start`, and `end` "
            "are all either timezone-aware or timezone-naive."
        ) from e

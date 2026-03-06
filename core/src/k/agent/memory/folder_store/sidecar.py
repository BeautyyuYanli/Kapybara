"""Filesystem sidecar index helpers for :mod:`k.agent.memory.folder`.

This module contains the sidecar-index/caching mechanics used by
`FolderMemoryStore` so the main store module can stay focused on
serialization/query behavior.

Design notes:
- The canonical source of truth remains `records/**/*.core.json` plus
  `records/**/*.detailed.jsonl`.
- Sidecar files under `index/` are derived, rebuildable acceleration data.
- Cache invalidation uses `index/records.epoch`; we additionally run a
  throttled drift probe that can auto-refresh when record files changed
  externally without touching epoch/index files.
- `index/stats.json` stores tiny aggregate metadata (count + latest record
  pointer) so hot paths avoid full scans and whole-file order reads.
"""

from __future__ import annotations

import json
import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from k.agent.memory.entities import MemoryRecord, datetime_to_posix_millis
from k.agent.memory.store import coerce_record_id


@dataclass(frozen=True, slots=True)
class _CacheKey:
    epoch_mtime_ns: int | None


@dataclass(frozen=True, slots=True)
class _IndexStats:
    record_count: int
    latest_id: str | None
    latest_core_relpath: str | None
    latest_bucket_relpath: str | None
    latest_record_mtime_ns: int | None


class FolderSidecarIndexMixin:
    """Provide filesystem sidecar-index lifecycle for `FolderMemoryStore`."""

    root: Path
    encoding: str

    _cache_key: _CacheKey | None
    _record_cache: OrderedDict[str, MemoryRecord]
    _meta_cache: OrderedDict[str, dict[str, object]]
    _record_paths: dict[str, Path]
    _bootstrapped: bool
    _last_drift_probe_ns: int
    _last_deep_drift_probe_ns: int

    _RECORD_CACHE_LIMIT: int
    _META_CACHE_LIMIT: int
    _AUTO_REFRESH_PROBE_INTERVAL_NS: int
    _AUTO_REFRESH_DEEP_PROBE_INTERVAL_NS: int

    def _init_sidecar_state(self) -> None:
        self._cache_key = None
        self._record_cache = OrderedDict()
        self._meta_cache = OrderedDict()
        self._record_paths = {}
        self._bootstrapped = False
        self._last_drift_probe_ns = 0
        self._last_deep_drift_probe_ns = 0

    def refresh(self) -> None:
        """Force a full on-disk index rebuild from canonical record files."""

        if not self.root.exists():
            self._clear_runtime_caches()
            self._cache_key = None
            self._bootstrapped = False
            return

        self._rebuild_index_from_disk()
        self._cache_key = self._stat_key()
        self._bootstrapped = True

    def _load_if_needed(self) -> None:
        if not self.root.exists():
            self._bootstrapped = False
            self._cache_key = None
            self._clear_runtime_caches()
            return

        if not self._bootstrapped:
            self._bootstrap_index_if_needed()
            self._bootstrapped = True

        key = self._stat_key()
        if self._cache_key is None or key != self._cache_key:
            self._clear_runtime_caches()
            self._cache_key = key

        self._maybe_auto_refresh_from_records_dir()

    def _bootstrap_index_if_needed(self) -> None:
        index_ready = (
            self._order_path().exists()
            and self._index_by_id_dir().exists()
            and self._stats_path().exists()
        )
        if not index_ready:
            self._rebuild_index_from_disk()
            return

        try:
            stats = self._read_stats()
        except ValueError:
            stats = None
        if stats is None:
            self._rebuild_index_from_disk()
            return

        self._ensure_epoch_file()
        self._cache_key = self._stat_key()

    def _rebuild_index_from_disk(self) -> None:
        records: list[MemoryRecord] = []
        by_id: dict[str, MemoryRecord] = {}
        record_paths: dict[str, Path] = {}

        for record_path in self._list_loadable_record_paths():
            try:
                record = self._load_record_from_record_path(record_path)
            except ValidationError as e:
                raise ValueError(
                    f"Invalid MemoryRecord JSON at {record_path}: {e}"
                ) from e
            except ValueError as e:
                raise ValueError(f"{e}") from e

            expected_id = self._expected_id_for_record_path(record_path)
            if record.id_ != expected_id:
                raise ValueError(
                    f"Record id mismatch at {record_path}: expected {expected_id}, got {record.id_}"
                )
            if record.id_ in by_id:
                existing_path = record_paths[record.id_]
                raise ValueError(
                    f"Duplicate MemoryRecord id on disk: {record.id_} ({existing_path}, {record_path})"
                )

            records.append(record)
            by_id[record.id_] = record
            record_paths[record.id_] = record_path

        records.sort(key=lambda record: record.id_)
        repaired_record_ids = self._repair_missing_links(
            records=records,
            by_id=by_id,
            missing_record_ids=set(),
        )

        if repaired_record_ids:
            self._record_paths = dict(record_paths)
            for record_id in sorted(repaired_record_ids):
                record_paths[record_id] = self._persist_record(
                    by_id[record_id], path_hint=record_paths.get(record_id)
                )

        tmp_index_dir = self.root / "index.tmp"
        if tmp_index_dir.exists():
            for old in sorted(tmp_index_dir.rglob("*"), reverse=True):
                if old.is_file():
                    old.unlink()
                elif old.is_dir():
                    old.rmdir()
            if tmp_index_dir.exists():
                tmp_index_dir.rmdir()
        (tmp_index_dir / "by-id").mkdir(parents=True, exist_ok=True)

        order_lines: list[str] = []
        for record in records:
            core_path = record_paths[record.id_]
            payload = self._meta_payload_for_record(record, core_path=core_path)
            meta_path = self._meta_path_for_id(
                record.id_, base_dir=tmp_index_dir / "by-id"
            )
            self._atomic_write_text(
                meta_path,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
            )
            order_lines.append(record.id_)

        self._atomic_write_text(
            tmp_index_dir / "order.ids",
            "".join(f"{id_}\n" for id_ in order_lines),
        )
        self._write_stats(
            self._build_stats_from_records(records=records, record_paths=record_paths),
            base_dir=tmp_index_dir,
        )

        index_dir = self._index_dir()
        if index_dir.exists():
            for old in sorted(index_dir.rglob("*"), reverse=True):
                if old.is_file():
                    old.unlink()
                elif old.is_dir():
                    old.rmdir()
            if index_dir.exists():
                index_dir.rmdir()
        tmp_index_dir.replace(index_dir)

        self._record_paths = dict(record_paths)
        self._touch_epoch()
        self._clear_runtime_caches()
        self._cache_key = self._stat_key()

    def _disk_record_count(self) -> int:
        return len(self._list_loadable_record_paths())

    def _index_record_count(self) -> int:
        by_id_dir = self._index_by_id_dir()
        if not by_id_dir.exists():
            return 0
        return sum(1 for _ in by_id_dir.rglob("*.json"))

    def _stat_key(self) -> _CacheKey | None:
        if not self.root.exists():
            return None

        epoch_path = self._epoch_path()
        if not epoch_path.exists():
            return _CacheKey(epoch_mtime_ns=None)
        try:
            stat = epoch_path.stat()
        except FileNotFoundError:
            return _CacheKey(epoch_mtime_ns=None)
        return _CacheKey(epoch_mtime_ns=stat.st_mtime_ns)

    def _index_dir(self) -> Path:
        return self.root / "index"

    def _index_by_id_dir(self) -> Path:
        return self._index_dir() / "by-id"

    def _order_path(self) -> Path:
        return self._index_dir() / "order.ids"

    def _epoch_path(self) -> Path:
        return self._index_dir() / "records.epoch"

    def _stats_path(self, *, base_dir: Path | None = None) -> Path:
        index_root = base_dir if base_dir is not None else self._index_dir()
        return index_root / "stats.json"

    def _ensure_epoch_file(self) -> None:
        epoch_path = self._epoch_path()
        if epoch_path.exists():
            return
        self._touch_epoch()

    def _touch_epoch(self) -> None:
        self._atomic_write_text(self._epoch_path(), f"{time.time_ns()}\n")

    def _serialize_stats(self, stats: _IndexStats) -> dict[str, object]:
        return {
            "record_count": stats.record_count,
            "latest_id": stats.latest_id,
            "latest_core_relpath": stats.latest_core_relpath,
            "latest_bucket_relpath": stats.latest_bucket_relpath,
            "latest_record_mtime_ns": stats.latest_record_mtime_ns,
        }

    def _parse_optional_relpath(
        self, *, payload: dict[str, object], key: str, stats_path: Path
    ) -> str | None:
        value = payload.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value:
            raise ValueError(
                f"Invalid {key} at {stats_path}: expected non-empty string"
            )
        return value

    def _read_stats(self) -> _IndexStats | None:
        stats_path = self._stats_path()
        if not stats_path.exists():
            return None
        try:
            payload = json.loads(stats_path.read_text(encoding=self.encoding))
        except OSError as e:
            raise ValueError(f"Failed to read index stats: {stats_path}: {e}") from e
        except ValueError as e:
            raise ValueError(f"Invalid index stats JSON at {stats_path}: {e}") from e

        if not isinstance(payload, dict):
            raise ValueError(
                f"Invalid index stats payload at {stats_path}: expected object"
            )

        record_count = payload.get("record_count")
        if not isinstance(record_count, int) or record_count < 0:
            raise ValueError(f"Invalid record_count in index stats at {stats_path}")

        latest_id_raw = payload.get("latest_id")
        latest_id: str | None
        if latest_id_raw is None:
            latest_id = None
        elif isinstance(latest_id_raw, str):
            latest_id = coerce_record_id(latest_id_raw)
        else:
            raise ValueError(f"Invalid latest_id in index stats at {stats_path}")

        latest_core_relpath = self._parse_optional_relpath(
            payload=payload,
            key="latest_core_relpath",
            stats_path=stats_path,
        )
        latest_bucket_relpath = self._parse_optional_relpath(
            payload=payload,
            key="latest_bucket_relpath",
            stats_path=stats_path,
        )
        latest_record_mtime_ns_raw = payload.get("latest_record_mtime_ns")
        latest_record_mtime_ns: int | None
        if latest_record_mtime_ns_raw is None:
            latest_record_mtime_ns = None
        elif (
            isinstance(latest_record_mtime_ns_raw, int)
            and latest_record_mtime_ns_raw > 0
        ):
            latest_record_mtime_ns = latest_record_mtime_ns_raw
        else:
            raise ValueError(
                f"Invalid latest_record_mtime_ns in index stats at {stats_path}"
            )

        if record_count == 0:
            if (
                latest_id is not None
                or latest_core_relpath is not None
                or latest_bucket_relpath is not None
                or latest_record_mtime_ns is not None
            ):
                raise ValueError(
                    f"Invalid index stats at {stats_path}: empty index must not define latest pointers"
                )
            return _IndexStats(
                record_count=0,
                latest_id=None,
                latest_core_relpath=None,
                latest_bucket_relpath=None,
                latest_record_mtime_ns=None,
            )

        if (
            latest_id is None
            or latest_core_relpath is None
            or latest_bucket_relpath is None
            or latest_record_mtime_ns is None
        ):
            raise ValueError(
                f"Invalid index stats at {stats_path}: non-empty index requires latest pointers"
            )

        return _IndexStats(
            record_count=record_count,
            latest_id=latest_id,
            latest_core_relpath=latest_core_relpath,
            latest_bucket_relpath=latest_bucket_relpath,
            latest_record_mtime_ns=latest_record_mtime_ns,
        )

    def _write_stats(self, stats: _IndexStats, *, base_dir: Path | None = None) -> None:
        self._atomic_write_text(
            self._stats_path(base_dir=base_dir),
            json.dumps(
                self._serialize_stats(stats),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n",
        )

    def _build_stats_from_records(
        self,
        *,
        records: list[MemoryRecord],
        record_paths: dict[str, Path],
    ) -> _IndexStats:
        if not records:
            return _IndexStats(
                record_count=0,
                latest_id=None,
                latest_core_relpath=None,
                latest_bucket_relpath=None,
                latest_record_mtime_ns=None,
            )

        latest_id = records[-1].id_
        latest_core_path = record_paths[latest_id]
        latest_mtime_ns = self._record_files_latest_mtime_ns(latest_core_path)
        if latest_mtime_ns is None:
            raise ValueError(f"Missing latest record files for id {latest_id}")
        return _IndexStats(
            record_count=len(records),
            latest_id=latest_id,
            latest_core_relpath=str(latest_core_path.relative_to(self.root)),
            latest_bucket_relpath=str(latest_core_path.parent.relative_to(self.root)),
            latest_record_mtime_ns=latest_mtime_ns,
        )

    def _meta_path_for_id(self, id_: str, *, base_dir: Path | None = None) -> Path:
        index_root = base_dir if base_dir is not None else self._index_by_id_dir()
        return index_root / id_[:2] / f"{id_}.json"

    def _iter_order_ids(self) -> Iterator[str]:
        order_path = self._order_path()
        if not order_path.exists():
            return
        try:
            with order_path.open(encoding=self.encoding) as fh:
                for raw_line in fh:
                    line = raw_line.strip()
                    if line:
                        yield line
        except OSError as e:
            raise ValueError(f"Failed to read order index: {order_path}: {e}") from e

    def _iter_order_ids_desc(self) -> Iterator[str]:
        order_path = self._order_path()
        if not order_path.exists():
            return

        try:
            with order_path.open("rb") as fh:
                fh.seek(0, 2)
                pos = fh.tell()
                tail = b""

                while pos > 0:
                    read_size = min(8192, pos)
                    pos -= read_size
                    fh.seek(pos)
                    chunk = fh.read(read_size)
                    parts = (chunk + tail).split(b"\n")
                    tail = parts[0]
                    for raw in reversed(parts[1:]):
                        line = raw.decode(self.encoding).strip()
                        if line:
                            yield line

                if tail:
                    line = tail.decode(self.encoding).strip()
                    if line:
                        yield line
        except OSError as e:
            raise ValueError(f"Failed to read order index: {order_path}: {e}") from e

    def _read_order_ids(self) -> list[str]:
        return list(self._iter_order_ids())

    def _insert_id_into_order(self, id_: str) -> None:
        order_path = self._order_path()
        stats: _IndexStats | None = None
        try:
            stats = self._read_stats()
        except ValueError:
            stats = None

        if stats is not None and order_path.exists():
            latest_id = stats.latest_id
            if latest_id is None or id_ > latest_id:
                order_path.parent.mkdir(parents=True, exist_ok=True)
                with order_path.open("a", encoding=self.encoding) as fh:
                    fh.write(f"{id_}\n")
                return

        ids = self._read_order_ids()
        if id_ in ids:
            return
        if not ids or id_ > ids[-1]:
            order_path.parent.mkdir(parents=True, exist_ok=True)
            with order_path.open("a", encoding=self.encoding) as fh:
                fh.write(f"{id_}\n")
            return

        ids.append(id_)
        ids.sort()
        self._atomic_write_text(
            self._order_path(),
            "".join(f"{line}\n" for line in ids),
        )

    def _update_stats_after_append(self, *, id_: str, core_path: Path) -> None:
        try:
            stats = self._read_stats()
        except ValueError:
            stats = None
        if stats is None:
            self._rebuild_index_from_disk()
            return

        latest_id = stats.latest_id
        latest_core_relpath = stats.latest_core_relpath
        latest_bucket_relpath = stats.latest_bucket_relpath
        latest_record_mtime_ns = stats.latest_record_mtime_ns
        record_count = stats.record_count + 1

        if latest_id is None or id_ > latest_id:
            latest_id = id_
            latest_core_relpath = str(core_path.relative_to(self.root))
            latest_bucket_relpath = str(core_path.parent.relative_to(self.root))
            latest_record_mtime_ns = self._record_files_latest_mtime_ns(core_path)
        else:
            # The latest record may still have been touched indirectly (for
            # example when it becomes a parent whose children list is updated).
            if latest_core_relpath is not None:
                latest_core_path = self.root / latest_core_relpath
                latest_record_mtime_ns = self._record_files_latest_mtime_ns(
                    latest_core_path
                )

        if (
            latest_id is None
            or latest_core_relpath is None
            or latest_bucket_relpath is None
            or latest_record_mtime_ns is None
        ):
            self._rebuild_index_from_disk()
            return

        self._write_stats(
            _IndexStats(
                record_count=record_count,
                latest_id=latest_id,
                latest_core_relpath=latest_core_relpath,
                latest_bucket_relpath=latest_bucket_relpath,
                latest_record_mtime_ns=latest_record_mtime_ns,
            )
        )

    def _meta_payload_for_record(
        self,
        record: MemoryRecord,
        *,
        core_path: Path,
    ) -> dict[str, object]:
        detailed_path = self._detailed_path_for_record_path(core_path)
        return {
            "id": record.id_,
            "created_at_ms": datetime_to_posix_millis(record.created_at),
            "in_channel": record.in_channel,
            "contacts": list(record.contacts),
            "parents": list(record.parents),
            "children": list(record.children),
            "core_relpath": str(core_path.relative_to(self.root)),
            "detailed_relpath": str(detailed_path.relative_to(self.root)),
        }

    def _validate_list_of_strings(
        self,
        *,
        record_id: str,
        field_name: str,
        value: object,
    ) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise ValueError(
                f"Invalid metadata field {field_name!r} for record id {record_id}"
            )
        return [str(v) for v in value]

    def _read_meta(self, id_: str) -> dict[str, object] | None:
        cached = self._meta_cache.get(id_)
        if cached is not None:
            self._meta_cache.move_to_end(id_)
            return dict(cached)

        meta_path = self._meta_path_for_id(id_)
        if not meta_path.exists():
            return None

        try:
            payload = json.loads(meta_path.read_text(encoding=self.encoding))
        except OSError as e:
            raise ValueError(f"Failed to read metadata at {meta_path}: {e}") from e
        except ValueError as e:
            raise ValueError(f"Invalid metadata JSON at {meta_path}: {e}") from e

        if not isinstance(payload, dict):
            raise ValueError(
                f"Invalid metadata payload at {meta_path}: expected object"
            )

        payload_id = payload.get("id")
        if not isinstance(payload_id, str):
            raise ValueError(f"Invalid metadata id at {meta_path}")
        coerce_record_id(payload_id)
        if payload_id != id_:
            raise ValueError(
                f"Metadata id mismatch at {meta_path}: expected {id_}, got {payload_id}"
            )

        created_at_ms = payload.get("created_at_ms")
        if not isinstance(created_at_ms, int):
            raise ValueError(f"Invalid created_at_ms at {meta_path}: expected int")
        in_channel = payload.get("in_channel")
        if not isinstance(in_channel, str):
            raise ValueError(f"Invalid in_channel at {meta_path}: expected string")

        contacts = self._validate_list_of_strings(
            record_id=id_,
            field_name="contacts",
            value=payload.get("contacts", []),
        )
        parents = self._validate_list_of_strings(
            record_id=id_,
            field_name="parents",
            value=payload.get("parents", []),
        )
        children = self._validate_list_of_strings(
            record_id=id_,
            field_name="children",
            value=payload.get("children", []),
        )

        for parent_id in parents:
            coerce_record_id(parent_id)
        for child_id in children:
            coerce_record_id(child_id)

        core_relpath = payload.get("core_relpath")
        detailed_relpath = payload.get("detailed_relpath")
        if not isinstance(core_relpath, str) or not isinstance(detailed_relpath, str):
            raise ValueError(
                f"Invalid core/detailed path metadata for record id {id_} at {meta_path}"
            )

        normalized = {
            "id": id_,
            "created_at_ms": created_at_ms,
            "in_channel": in_channel,
            "contacts": contacts,
            "parents": parents,
            "children": children,
            "core_relpath": core_relpath,
            "detailed_relpath": detailed_relpath,
        }
        self._meta_cache[id_] = normalized
        self._meta_cache.move_to_end(id_)
        while len(self._meta_cache) > self._META_CACHE_LIMIT:
            self._meta_cache.popitem(last=False)
        self._record_paths[id_] = self.root / core_relpath
        return dict(normalized)

    def _upsert_meta(self, record: MemoryRecord, *, core_path: Path) -> None:
        payload = self._meta_payload_for_record(record, core_path=core_path)
        meta_path = self._meta_path_for_id(record.id_)
        self._atomic_write_text(
            meta_path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
        )
        self._meta_cache[record.id_] = dict(payload)
        self._meta_cache.move_to_end(record.id_)
        while len(self._meta_cache) > self._META_CACHE_LIMIT:
            self._meta_cache.popitem(last=False)
        self._record_paths[record.id_] = core_path

    def _cache_record(self, record: MemoryRecord) -> None:
        self._record_cache[record.id_] = record
        self._record_cache.move_to_end(record.id_)
        while len(self._record_cache) > self._RECORD_CACHE_LIMIT:
            self._record_cache.popitem(last=False)

    def _get_by_id(self, record_id: str, *, allow_rebuild: bool) -> MemoryRecord | None:
        cached = self._record_cache.get(record_id)
        if cached is not None:
            self._record_cache.move_to_end(record_id)
            return cached

        meta = self._read_meta(record_id)
        if meta is None:
            return None

        core_relpath = meta.get("core_relpath")
        detailed_relpath = meta.get("detailed_relpath")
        if not isinstance(core_relpath, str) or not isinstance(detailed_relpath, str):
            raise ValueError(f"Invalid metadata paths for record id {record_id}")

        core_path = self.root / core_relpath
        detailed_path = self.root / detailed_relpath
        if not core_path.exists() or not detailed_path.exists():
            if not allow_rebuild:
                return None
            self._rebuild_index_from_disk()
            return self._get_by_id(record_id, allow_rebuild=False)

        try:
            record = self._load_record_from_record_path(core_path)
        except ValidationError as e:
            raise ValueError(f"Invalid MemoryRecord JSON at {core_path}: {e}") from e

        self._cache_record(record)
        self._record_paths[record_id] = core_path
        return record

    def _clear_runtime_caches(self) -> None:
        self._record_cache.clear()
        self._meta_cache.clear()
        self._record_paths = {}

    def _maybe_auto_refresh_from_records_dir(self) -> None:
        now_ns = time.monotonic_ns()
        if now_ns - self._last_drift_probe_ns < self._AUTO_REFRESH_PROBE_INTERVAL_NS:
            return
        self._last_drift_probe_ns = now_ns

        if self._quick_drift_detected():
            self.refresh()
            return

        if (
            now_ns - self._last_deep_drift_probe_ns
            < self._AUTO_REFRESH_DEEP_PROBE_INTERVAL_NS
        ):
            return
        self._last_deep_drift_probe_ns = now_ns

        if self._deep_drift_detected():
            self.refresh()

    def _quick_drift_detected(self) -> bool:
        try:
            stats = self._read_stats()
        except ValueError:
            stats = None
        if stats is None:
            return True

        if stats.record_count == 0:
            records_dir = self.root / "records"
            if not records_dir.exists():
                return False
            try:
                records_dir_mtime_ns = records_dir.stat().st_mtime_ns
            except FileNotFoundError:
                return False
            key = self._stat_key()
            epoch_mtime_ns = key.epoch_mtime_ns if key is not None else None
            return epoch_mtime_ns is None or records_dir_mtime_ns > epoch_mtime_ns

        if stats.latest_bucket_relpath is None:
            return True
        latest_bucket_path = self.root / stats.latest_bucket_relpath
        try:
            latest_bucket_mtime_ns = latest_bucket_path.stat().st_mtime_ns
        except FileNotFoundError:
            return True

        key = self._stat_key()
        epoch_mtime_ns = key.epoch_mtime_ns if key is not None else None
        if epoch_mtime_ns is None or latest_bucket_mtime_ns > epoch_mtime_ns:
            return True

        if stats.latest_core_relpath is None or stats.latest_record_mtime_ns is None:
            return True
        latest_core_path = self.root / stats.latest_core_relpath
        latest_record_mtime_ns = self._record_files_latest_mtime_ns(latest_core_path)
        if latest_record_mtime_ns is None:
            return True

        return latest_record_mtime_ns > stats.latest_record_mtime_ns

    def _deep_drift_detected(self) -> bool:
        if self._disk_record_count() != self._index_record_count():
            return True

        key = self._stat_key()
        epoch_mtime_ns = key.epoch_mtime_ns if key is not None else None
        latest_records_mtime_ns = self._latest_records_mtime_ns()
        if latest_records_mtime_ns is None:
            return False
        return epoch_mtime_ns is None or latest_records_mtime_ns > epoch_mtime_ns

    def _record_files_latest_mtime_ns(self, core_path: Path) -> int | None:
        try:
            latest = core_path.stat().st_mtime_ns
        except FileNotFoundError:
            return None

        detailed_path = self._detailed_path_for_record_path(core_path)
        try:
            detailed_mtime_ns = detailed_path.stat().st_mtime_ns
        except FileNotFoundError:
            return latest
        return max(latest, detailed_mtime_ns)

    def _latest_records_mtime_ns(self) -> int | None:
        latest: int | None = None
        for record_path in self._list_loadable_record_paths():
            record_latest_mtime_ns = self._record_files_latest_mtime_ns(record_path)
            if record_latest_mtime_ns is None:
                continue
            latest = (
                record_latest_mtime_ns
                if latest is None
                else max(latest, record_latest_mtime_ns)
            )
        return latest

    # Hook methods expected from FolderMemoryStore
    def _list_loadable_record_paths(self) -> list[Path]:  # pragma: no cover
        raise NotImplementedError

    def _load_record_from_record_path(
        self, record_path: Path
    ) -> MemoryRecord:  # pragma: no cover
        raise NotImplementedError

    def _persist_record(
        self, record: MemoryRecord, *, path_hint: Path | None
    ) -> Path:  # pragma: no cover
        raise NotImplementedError

    def _repair_missing_links(
        self,
        *,
        records: list[MemoryRecord],
        by_id: dict[str, MemoryRecord],
        missing_record_ids: set[str],
    ) -> set[str]:  # pragma: no cover
        raise NotImplementedError

    def _expected_id_for_record_path(self, path: Path) -> str:  # pragma: no cover
        raise NotImplementedError

    def _detailed_path_for_record_path(
        self, record_path: Path
    ) -> Path:  # pragma: no cover
        raise NotImplementedError

    def _atomic_write_text(self, path: Path, text: str) -> None:  # pragma: no cover
        raise NotImplementedError

"""Base sidecar-index lifecycle and order/index-file helpers.

Design notes:
- The canonical source of truth remains `records/**/*.core.json` plus
  `records/**/*.detailed.jsonl`.
- Sidecar files under `index/` are derived, rebuildable acceleration data.
- Canonical writes and sidecar rebuilds share one advisory lock under the
  store root so cross-process append/rebuild operations never overlap.
- Cache invalidation uses `index/records.epoch`.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from k.agent.memory.entities import MemoryRecord


@dataclass(frozen=True, slots=True)
class _CacheKey:
    epoch_mtime_ns: int | None


@dataclass(slots=True)
class _ProcessStoreLockState:
    """Per-process bookkeeping for a root-scoped advisory store lock."""

    mutex: threading.RLock
    fd: int | None = None
    depth: int = 0


_PROCESS_STORE_LOCKS: dict[str, _ProcessStoreLockState] = {}
_PROCESS_STORE_LOCKS_GUARD = threading.Lock()


def _process_store_lock_state(lock_path: Path) -> _ProcessStoreLockState:
    """Return shared in-process lock state for `lock_path`."""

    key = str(lock_path)
    with _PROCESS_STORE_LOCKS_GUARD:
        state = _PROCESS_STORE_LOCKS.get(key)
        if state is None:
            state = _ProcessStoreLockState(mutex=threading.RLock())
            _PROCESS_STORE_LOCKS[key] = state
        return state


class FolderSidecarBaseMixin:
    """Provide base filesystem sidecar-index lifecycle for `FolderMemoryStore`."""

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

    if TYPE_CHECKING:
        from k.agent.memory.folder_store.sidecar.stats import _IndexStats

        def _stats_path(self, *, base_dir: Path | None = None) -> Path: ...
        def _read_stats(self) -> _IndexStats | None: ...
        def _write_stats(
            self, stats: _IndexStats, *, base_dir: Path | None = None
        ) -> None: ...
        def _build_stats_from_records(
            self,
            *,
            records: list[MemoryRecord],
            record_paths: dict[str, Path],
        ) -> _IndexStats: ...
        def _meta_payload_for_record(
            self, record: MemoryRecord, *, core_path: Path
        ) -> dict[str, object]: ...
        def _maybe_auto_refresh_from_records_dir(self) -> None: ...

    def _init_sidecar_state(self) -> None:
        self._cache_key = None
        self._record_cache = OrderedDict()
        self._meta_cache = OrderedDict()
        self._record_paths = {}
        self._bootstrapped = False
        self._last_drift_probe_ns = 0
        self._last_deep_drift_probe_ns = 0

    def refresh(self) -> None:
        """Force a full on-disk index rebuild from canonical record files.

        Rebuilds take the store's advisory write lock so concurrent processes do
        not mutate or swap derived sidecar directories at the same time.
        """

        if not self.root.exists():
            self._clear_runtime_caches()
            self._cache_key = None
            self._bootstrapped = False
            return

        self._rebuild_index_from_disk()
        self._cache_key = self._stat_key()
        self._bootstrapped = True

    def _store_lock_path(self) -> Path:
        return self.root / ".folder-memory-store.lock"

    @contextmanager
    def _exclusive_store_lock(self) -> Iterator[None]:
        """Serialize cross-process canonical writes and sidecar rebuilds.

        We keep the critical section root-scoped instead of sidecar-scoped
        because `append()` first mutates canonical record files and only then
        refreshes derived `index/` data. Readers that trigger rebuilds must not
        scan the canonical tree while that write is mid-flight.
        """

        lock_path = self._store_lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        state = _process_store_lock_state(lock_path)
        with state.mutex:
            if state.depth == 0:
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                except BaseException:
                    os.close(fd)
                    raise
                state.fd = fd
            state.depth += 1
            try:
                yield
            finally:
                state.depth -= 1
                if state.depth == 0:
                    fd = state.fd
                    state.fd = None
                    if fd is not None:
                        try:
                            fcntl.flock(fd, fcntl.LOCK_UN)
                        finally:
                            os.close(fd)

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
        with self._exclusive_store_lock():
            self._rebuild_index_from_disk_unlocked()

    def _rebuild_index_from_disk_unlocked(self) -> None:
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

    def _ensure_epoch_file(self) -> None:
        epoch_path = self._epoch_path()
        if epoch_path.exists():
            return
        self._touch_epoch()

    def _touch_epoch(self) -> None:
        self._atomic_write_text(self._epoch_path(), f"{time.time_ns()}\n")

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
        stats = None
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

    def _clear_runtime_caches(self) -> None:
        self._record_cache.clear()
        self._meta_cache.clear()
        self._record_paths = {}

    # Hooks from FolderMemoryStore
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

    def _atomic_write_text(self, path: Path, text: str) -> None:  # pragma: no cover
        raise NotImplementedError

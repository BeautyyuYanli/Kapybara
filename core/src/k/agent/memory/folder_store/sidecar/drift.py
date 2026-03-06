"""Auto-refresh drift probe helpers.

The probe is two-stage:
- Fast probe: cheap O(1)-ish checks using stats/mtime hints.
- Deep probe: O(N) validation run at a slower cadence.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING


class FolderSidecarDriftMixin:
    root: Path

    _last_drift_probe_ns: int
    _last_deep_drift_probe_ns: int

    _AUTO_REFRESH_PROBE_INTERVAL_NS: int
    _AUTO_REFRESH_DEEP_PROBE_INTERVAL_NS: int

    if TYPE_CHECKING:
        from k.agent.memory.folder_store.sidecar.base import _CacheKey
        from k.agent.memory.folder_store.sidecar.stats import _IndexStats

        def refresh(self) -> None: ...
        def _read_stats(self) -> _IndexStats | None: ...
        def _stat_key(self) -> _CacheKey | None: ...
        def _disk_record_count(self) -> int: ...
        def _index_record_count(self) -> int: ...
        def _list_loadable_record_paths(self) -> list[Path]: ...
        def _detailed_path_for_record_path(self, record_path: Path) -> Path: ...

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

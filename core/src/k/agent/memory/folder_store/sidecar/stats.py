"""Index stats sidecar read/write helpers.

`index/stats.json` stores tiny aggregate metadata so append/order hot paths can
avoid full scans while preserving rebuildability from canonical record files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from k.agent.memory.entities import MemoryRecord
from k.agent.memory.store import coerce_record_id


@dataclass(frozen=True, slots=True)
class _IndexStats:
    record_count: int
    latest_id: str | None
    latest_core_relpath: str | None
    latest_bucket_relpath: str | None
    latest_record_mtime_ns: int | None


class FolderSidecarStatsMixin:
    root: Path
    encoding: str

    def _stats_path(self, *, base_dir: Path | None = None) -> Path:
        index_root = base_dir if base_dir is not None else self._index_dir()
        return index_root / "stats.json"

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

    # Hooks from sibling mixins / FolderMemoryStore
    def _index_dir(self) -> Path:  # pragma: no cover
        raise NotImplementedError

    def _atomic_write_text(self, path: Path, text: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _record_files_latest_mtime_ns(
        self, core_path: Path
    ) -> int | None:  # pragma: no cover
        raise NotImplementedError

    def _rebuild_index_from_disk(self) -> None:  # pragma: no cover
        raise NotImplementedError

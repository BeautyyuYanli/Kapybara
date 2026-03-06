"""Per-record metadata index and record-cache helpers."""

from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

from pydantic import ValidationError

from k.agent.memory.entities import MemoryRecord, datetime_to_posix_millis
from k.agent.memory.store import coerce_record_id


class FolderSidecarMetadataMixin:
    root: Path
    encoding: str

    _record_cache: OrderedDict[str, MemoryRecord]
    _meta_cache: OrderedDict[str, dict[str, object]]
    _record_paths: dict[str, Path]

    _RECORD_CACHE_LIMIT: int
    _META_CACHE_LIMIT: int

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

    # Hooks from sibling mixins / FolderMemoryStore
    def _meta_path_for_id(
        self, id_: str, *, base_dir: Path | None = None
    ) -> Path:  # pragma: no cover
        raise NotImplementedError

    def _atomic_write_text(self, path: Path, text: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def _detailed_path_for_record_path(
        self, record_path: Path
    ) -> Path:  # pragma: no cover
        raise NotImplementedError

    def _load_record_from_record_path(
        self, record_path: Path
    ) -> MemoryRecord:  # pragma: no cover
        raise NotImplementedError

    def _rebuild_index_from_disk(self) -> None:  # pragma: no cover
        raise NotImplementedError

"""Folder-backed storage for :class:`k.agent.memory.entities.MemoryRecord`.

This store persists one record per file under a root folder, plus an append-order
index file.

Layout (relative to `root`):
- `order.jsonl`: one JSON object per non-empty line (append order), storing the
  record id, `created_at`, and relative path.
- `records/YYYY/MM/DD/HH/<id>.core.json`: one JSON blob per record (one line),
  storing record metadata and `compacted`.
- `records/YYYY/MM/DD/HH/<id>.detailed.jsonl`: a JSONL file (one JSON value per
  non-empty line). Line 1 is the raw `input` (a JSON string). Line 2 is the
  record `output` (a JSON string). Each subsequent non-empty line corresponds
  to one `ModelResponse` and is a JSON array of simplified tool call parts
  extracted from that response. Each element is an object with only `tool_name`
  and `args`. `ModelRequest` messages and full `ModelResponse` objects are not
  persisted in this detailed file.

Design notes / invariants:
- "Latest" means the last id in `order.jsonl` (append order), not necessarily the
  max `created_at`.
- Parsing is strict: invalid ids in `order.jsonl`, invalid JSON, or invalid
  `MemoryRecord` data raises `ValueError` with path/line context.
- `MemoryRecord` loading expects channel fields (`in_channel`, optional
  `out_channel`). Legacy `kind`-only records are not read directly; migrate them
  first via `k.agent.memory.folder_migrate_kind_to_channel`.
- `append()` updates each referenced parent's `children` list (persisting parent
  records) before persisting the new record.
- Cache invalidation is keyed off `order.jsonl` mtime/size. If record files are
  modified externally without updating `order.jsonl`, call `refresh()`.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Set
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.messages import BaseToolCallPart, ModelResponse

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


@dataclass(frozen=True, slots=True)
class _OrderEntry:
    id_: str
    created_at: datetime
    relpath: str


_CORE_FIELDS: set[str] = {
    "created_at",
    "in_channel",
    "out_channel",
    "id_",
    "parents",
    "children",
    "compacted",
}


class _CoreRecordOnDisk(BaseModel):
    """On-disk schema for `<id>.core.json` in the split core/detailed format."""

    created_at: datetime
    in_channel: str
    out_channel: str | None = None
    id_: str
    parents: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)
    compacted: list[str] = Field(default_factory=list)


def _compacted_sidecar_path_for_record_path(record_path: Path) -> Path:
    """Return the legacy `*.compacted.json` sidecar path for `record_path`."""

    name = record_path.name
    if name.endswith(".core.json"):
        record_id = name[: -len(".core.json")]
    elif name.endswith(".json") and not name.endswith(".detailed.json"):
        record_id = name[: -len(".json")]
    else:
        raise ValueError(f"Unexpected record filename: {record_path}")
    return record_path.with_name(f"{record_id}.compacted.json")


def _read_detailed_file(
    path: Path, *, encoding: str
) -> tuple[str, str, list[list[dict[str, object]]]]:
    """Read `<id>.detailed.jsonl` JSONL as `(input, output, tool_calls_by_response)`."""

    try:
        lines = path.read_text(encoding=encoding).splitlines()
    except OSError as e:
        raise ValueError(f"Failed to read detailed file: {path}: {e}") from e

    input_line_no: int | None = None
    input_value: str | None = None
    output_line_no: int | None = None
    output_value: str | None = None
    tool_calls_by_response: list[list[dict[str, object]]] = []

    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line:
            continue

        if input_value is None:
            input_line_no = line_no
            try:
                decoded = json.loads(line)
            except ValueError as e:
                raise ValueError(f"Invalid JSON at {path}:{line_no}: {e}") from e
            if not isinstance(decoded, str):
                raise ValueError(
                    f"Invalid detailed file at {path}:{line_no}: first JSON value must be a string"
                )
            input_value = decoded
            continue

        if output_value is None:
            output_line_no = line_no
            try:
                decoded = json.loads(line)
            except ValueError as e:
                raise ValueError(f"Invalid JSON at {path}:{line_no}: {e}") from e
            if not isinstance(decoded, str):
                raise ValueError(
                    f"Invalid detailed file at {path}:{line_no}: second JSON value must be a string"
                )
            output_value = decoded
            continue

        try:
            decoded = json.loads(line)
        except ValueError as e:
            raise ValueError(f"Invalid JSON at {path}:{line_no}: {e}") from e
        if not isinstance(decoded, list):
            raise ValueError(
                f"Invalid detailed file at {path}:{line_no}: expected a JSON array for response tool calls"
            )
        tool_calls: list[dict[str, object]] = []
        for idx, item in enumerate(decoded):
            if not isinstance(item, dict):
                raise ValueError(
                    f"Invalid detailed file at {path}:{line_no}: tool_calls[{idx}] must be an object"
                )
            tool_name = item.get("tool_name")
            if not isinstance(tool_name, str) or not tool_name:
                raise ValueError(
                    f"Invalid detailed file at {path}:{line_no}: tool_calls[{idx}].tool_name must be a non-empty string"
                )
            args = item.get("args")
            if args is not None and not isinstance(args, (str, dict)):
                raise ValueError(
                    f"Invalid detailed file at {path}:{line_no}: tool_calls[{idx}].args must be a string, object, or null"
                )
            tool_calls.append({"tool_name": tool_name, "args": args})
        tool_calls_by_response.append(tool_calls)

    if input_value is None:
        suffix = "" if input_line_no is None else f":{input_line_no}"
        raise ValueError(
            f"Invalid detailed file at {path}{suffix}: missing raw input line"
        )

    if output_value is None:
        suffix = "" if output_line_no is None else f":{output_line_no}"
        raise ValueError(
            f"Invalid detailed file at {path}{suffix}: missing output line"
        )

    return input_value, output_value, tool_calls_by_response


def _encode_detailed_jsonl(record: MemoryRecord) -> str:
    """Encode a record's detailed data as JSONL (input + output + tool_calls per response)."""

    lines: list[str] = [
        json.dumps(record.input, ensure_ascii=False),
        json.dumps(record.output, ensure_ascii=False),
    ]

    for msg in record.detailed:
        if not isinstance(msg, ModelResponse):
            continue
        tool_calls: list[dict[str, object]] = []
        for part in msg.parts:
            if isinstance(part, BaseToolCallPart):
                tool_calls.append({"tool_name": part.tool_name, "args": part.args})
        lines.append(json.dumps(tool_calls, ensure_ascii=False, separators=(",", ":")))

    return "\n".join(lines) + "\n"


def _load_memory_record_from_disk(
    record_path: Path,
    raw_core: str,
    *,
    encoding: str,
    detailed_path: Path,
) -> MemoryRecord:
    """Load a `MemoryRecord` from disk, supporting legacy and split formats.

    Legacy formats:
    - `<id>.core.json` or `<id>.json` storing most fields (including `input`).
    - Optional sibling `<id>.compacted.json` sidecar storing `compacted`.

    Split format:
    - `<id>.core.json` stores metadata + `compacted`.
    - `<id>.detailed.jsonl` stores raw `input` + `output` + per-response tool-call lists.
    """

    try:
        decoded = json.loads(raw_core)
    except ValueError as e:
        raise ValueError(f"Invalid JSON at {record_path}: {e}") from e

    if not isinstance(decoded, dict):
        raise ValueError(f"Invalid MemoryRecord JSON at {record_path}: expected object")

    # Legacy core files include `input`.
    if "input" in decoded:
        try:
            record = MemoryRecord.model_validate(decoded)
        except ValidationError as e:
            raise e

        if "compacted" not in decoded:
            compacted_path = _compacted_sidecar_path_for_record_path(record_path)
            if compacted_path.exists():
                record = record.model_copy(
                    update={
                        "compacted": _read_legacy_compacted_sidecar(
                            compacted_path, encoding=encoding
                        )
                    }
                )

        return record

    core = _CoreRecordOnDisk.model_validate(decoded)

    # Backward compatibility: some stores used a `*.compacted.json` sidecar.
    if "compacted" not in decoded:
        compacted_path = _compacted_sidecar_path_for_record_path(record_path)
        if compacted_path.exists():
            core.compacted = _read_legacy_compacted_sidecar(
                compacted_path, encoding=encoding
            )

    if not detailed_path.exists():
        raise ValueError(f"Missing detailed file for id {core.id_}: {detailed_path}")

    input_value, output_value, _tool_calls_by_response = _read_detailed_file(
        detailed_path, encoding=encoding
    )
    return MemoryRecord(
        created_at=core.created_at,
        in_channel=core.in_channel,
        out_channel=core.out_channel,
        id_=core.id_,
        parents=list(core.parents),
        children=list(core.children),
        input=input_value,
        compacted=list(core.compacted),
        output=output_value,
        detailed=[],
    )


def _read_legacy_compacted_sidecar(path: Path, *, encoding: str) -> list[str]:
    try:
        raw = path.read_text(encoding=encoding)
    except OSError as e:
        raise ValueError(f"Failed to read compacted sidecar: {path}: {e}") from e
    try:
        decoded = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"Invalid JSON at {path}: {e}") from e
    if not isinstance(decoded, list) or any(
        not isinstance(item, str) for item in decoded
    ):
        raise ValueError(
            f"Invalid compacted sidecar at {path}: expected JSON array of strings"
        )
    return decoded


def _read_record_id_and_created_at(raw: str, *, path: Path) -> tuple[str, datetime]:
    """Return `(id_, created_at)` for a record file (legacy or split core)."""

    try:
        decoded = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"Invalid JSON at {path}: {e}") from e

    if not isinstance(decoded, dict):
        raise ValueError(f"Invalid MemoryRecord JSON at {path}: expected object")

    raw_id = decoded.get("id_") or decoded.get("id")
    if not isinstance(raw_id, str):
        raise ValueError(f"Invalid MemoryRecord JSON at {path}: missing/invalid 'id_'")
    record_id = coerce_record_id(raw_id)

    raw_created_at = decoded.get("created_at")
    if not isinstance(raw_created_at, str):
        raise ValueError(
            f"Invalid MemoryRecord JSON at {path}: missing/invalid 'created_at'"
        )
    try:
        created_at = datetime.fromisoformat(raw_created_at)
    except ValueError as e:
        raise ValueError(
            f"Invalid MemoryRecord JSON at {path}: invalid 'created_at': {e}"
        ) from e

    return record_id, created_at


class FolderMemoryStore(MemoryStore):
    """Query and append `MemoryRecord` objects stored in a folder."""

    root: Path
    encoding: str

    _cache_key: _CacheKey | None
    _records: list[MemoryRecord]
    _by_id: dict[str, MemoryRecord]
    _record_paths: dict[str, Path]

    def __init__(self, root: str | Path, *, encoding: str = "utf-8") -> None:
        self.root = Path(root)
        self.encoding = encoding
        self._cache_key = None
        self._records = []
        self._by_id = {}
        self._record_paths = {}

    def refresh(self) -> None:
        """Force a reload from disk (even if `order.jsonl` did not change)."""

        self._cache_key = None
        self._load_if_needed()

    def get_latest(self) -> str | None:
        self._load_if_needed()
        return self._records[-1].id_ if self._records else None

    def get_by_id(self, id_: MemoryRecordId) -> MemoryRecord | None:
        self._load_if_needed()
        record_id = coerce_record_id(id_)
        return self._by_id.get(record_id)

    def get_by_ids(
        self, ids: Set[MemoryRecordId], *, strict: bool = False
    ) -> list[MemoryRecord]:
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
        self._load_if_needed()

        if record.id_ in self._by_id:
            raise ValueError(
                f"Duplicate MemoryRecord id encountered while appending: {record.id_}"
            )

        updated_parents: list[MemoryRecord] = []
        for parent_id in record.parents:
            parent = self._by_id.get(parent_id)
            if parent is None:
                raise KeyError(f"Unknown parent MemoryRecord id: {parent_id}")
            if record.id_ not in parent.children:
                parent.children.append(record.id_)
                updated_parents.append(parent)

        for parent in updated_parents:
            self._persist_record(parent)

        record_path = self._persist_record(record)
        self._append_order_line(record, record_path)

        self._records.append(record)
        self._by_id[record.id_] = record
        self._cache_key = self._stat_key()

    def _detailed_path_for_record_path(self, record_path: Path) -> Path:
        """Return the sibling detailed path for a record path.

        The record path may be the canonical `<id>.core.json` file or a legacy
        `<id>.json` file referenced by old `order.jsonl` entries.
        """

        name = record_path.name
        if name.endswith(".core.json"):
            record_id = name[: -len(".core.json")]
        elif name.endswith(".json") and not name.endswith(
            (".detailed.json", ".detailed.jsonl")
        ):
            record_id = name[: -len(".json")]
        else:
            raise ValueError(f"Unexpected record filename: {record_path}")
        return record_path.with_name(f"{record_id}.detailed.jsonl")

    def _load_if_needed(self) -> None:
        if not self.root.exists():
            self._cache_key = None
            self._records = []
            self._by_id = {}
            self._record_paths = {}
            return

        order_path = self._order_path()
        if not order_path.exists():
            self._rebuild_order_from_records()

        key = self._stat_key()
        if key is None:
            self._cache_key = None
            self._records = []
            self._by_id = {}
            self._record_paths = {}
            return

        if self._cache_key is not None and key == self._cache_key:
            return

        order_entries = _read_order_file(self._order_path(), encoding=self.encoding)

        records: list[MemoryRecord] = []
        by_id: dict[str, MemoryRecord] = {}
        record_paths: dict[str, Path] = {}
        for entry in order_entries:
            record_path = self._resolve_record_path(entry)
            if not record_path.exists():
                # Backward compatibility for relpaths written before the
                # "<id>.core.json" convention.
                if record_path.name.endswith(".core.json"):
                    legacy = record_path.with_name(
                        record_path.name[: -len(".core.json")] + ".json"
                    )
                    if legacy.exists():
                        record_path = legacy
                elif record_path.name.endswith(
                    ".json"
                ) and not record_path.name.endswith(
                    (".detailed.json", ".detailed.jsonl")
                ):
                    core = record_path.with_name(
                        record_path.name[: -len(".json")] + ".core.json"
                    )
                    if core.exists():
                        record_path = core
            try:
                raw = record_path.read_text(encoding=self.encoding)
            except FileNotFoundError as e:
                raise ValueError(
                    f"Missing record file for id {entry.id_}: {record_path}"
                ) from e
            try:
                record = _load_memory_record_from_disk(
                    record_path,
                    raw,
                    encoding=self.encoding,
                    detailed_path=self._detailed_path_for_record_path(record_path),
                )
            except ValidationError as e:
                raise ValueError(
                    f"Invalid MemoryRecord JSON at {record_path}: {e}"
                ) from e
            except ValueError as e:
                raise ValueError(f"{e}") from e

            if record.id_ != entry.id_:
                raise ValueError(
                    f"Record id mismatch at {record_path}: expected {entry.id_}, got {record.id_}"
                )
            if record.id_ in by_id:
                raise ValueError(
                    f"Duplicate MemoryRecord id in order file: {record.id_}"
                )

            records.append(record)
            by_id[record.id_] = record
            record_paths[record.id_] = record_path

        self._records = records
        self._by_id = by_id
        self._record_paths = record_paths
        self._cache_key = key

    def _rebuild_order_from_records(self) -> None:
        records_dir = self._records_dir()
        if not records_dir.exists():
            self.root.mkdir(parents=True, exist_ok=True)
            self._persist_order([])
            return

        indexed: list[tuple[str, datetime, str]] = []
        for path in records_dir.rglob("*.json"):
            if path.name.endswith(".detailed.json"):
                continue
            if path.name.endswith(".detailed.jsonl"):
                continue
            if path.name.endswith(".compacted.json"):
                continue
            if (
                path.name.endswith(".json")
                and not path.name.endswith(".core.json")
                and (path.with_name(f"{path.stem}.core.json")).exists()
            ):
                # If both legacy "<id>.json" and "<id>.core.json" exist, the core
                # file is authoritative.
                continue
            try:
                raw = path.read_text(encoding=self.encoding)
            except OSError as e:
                raise ValueError(f"Failed to read MemoryRecord at {path}: {e}") from e
            try:
                record_id, created_at = _read_record_id_and_created_at(raw, path=path)
            except ValueError as e:
                raise ValueError(f"{e}") from e
            indexed.append((record_id, created_at, str(path.relative_to(self.root))))

        # Stable order for rebuilds: by created_at then id.
        indexed.sort(key=lambda t: (t[1], str(t[0])))
        self._persist_order(
            [
                _OrderEntry(
                    id_=record_id,
                    created_at=created_at,
                    relpath=relpath,
                )
                for record_id, created_at, relpath in indexed
            ]
        )

    def _stat_key(self) -> _CacheKey | None:
        try:
            stat = self._order_path().stat()
        except FileNotFoundError:
            return None
        return _CacheKey(mtime_ns=stat.st_mtime_ns, size=stat.st_size)

    def _order_path(self) -> Path:
        return self.root / "order.jsonl"

    def _records_dir(self) -> Path:
        return self.root / "records"

    def _record_path_for(self, record: MemoryRecord) -> Path:
        return self._record_path_for_id_and_created_at(record.id_, record.created_at)

    def _record_path_for_id_and_created_at(
        self, id_: str, created_at: datetime
    ) -> Path:
        return (
            self._records_dir()
            / f"{created_at.year:04d}"
            / f"{created_at.month:02d}"
            / f"{created_at.day:02d}"
            / f"{created_at.hour:02d}"
            / f"{id_}.core.json"
        )

    def _atomic_write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = path.parent if path.parent.exists() else None
        with tempfile.NamedTemporaryFile(
            "w",
            encoding=self.encoding,
            dir=tmp_dir,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tf:
            tmp_path = Path(tf.name)
            tf.write(text)
        tmp_path.replace(path)

    def _persist_record(self, record: MemoryRecord) -> Path:
        path = self._record_paths.get(record.id_)
        if path is None or not path.name.endswith(".core.json"):
            path = self._record_path_for(record)

        # Split persistence:
        # - core: metadata + channel routing + compacted (one JSON blob, one line)
        # - detailed: raw input + output + tool_calls per response (JSONL)
        self._atomic_write_text(
            path,
            record.model_dump_json(include=_CORE_FIELDS),
        )

        detailed_path = self._detailed_path_for_record_path(path)
        self._atomic_write_text(
            detailed_path,
            _encode_detailed_jsonl(record),
        )

        self._record_paths[record.id_] = path
        return path

    def _persist_order(self, entries: list[_OrderEntry]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        order_path = self._order_path()
        lines: list[str] = []
        for entry in entries:
            payload = {
                "id": str(entry.id_),
                "created_at": entry.created_at.isoformat(),
                "relpath": entry.relpath,
            }
            lines.append(json.dumps(payload))
        self._atomic_write_text(order_path, "\n".join(lines) + ("\n" if lines else ""))

    def _append_order_line(self, record: MemoryRecord, record_path: Path) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with self._order_path().open("a", encoding=self.encoding) as f:
            payload = {
                "id": str(record.id_),
                "created_at": record.created_at.isoformat(),
                "relpath": str(record_path.relative_to(self.root)),
            }
            f.write(json.dumps(payload) + "\n")

    def _coerce_record(self, record: MemoryRecordRef) -> MemoryRecord:
        if isinstance(record, MemoryRecord):
            return record
        record_id = coerce_record_id(record)
        rec = self._by_id.get(record_id)
        if rec is None:
            raise KeyError(f"Unknown MemoryRecord id: {record_id}")
        return rec

    def _resolve_record_path(self, entry: _OrderEntry) -> Path:
        relpath = Path(entry.relpath)
        if relpath.is_absolute() or ".." in relpath.parts:
            raise ValueError(f"Invalid relpath in order.jsonl: {entry.relpath!r}")
        return self.root / relpath


def _read_order_file(path: Path, *, encoding: str) -> list[_OrderEntry]:
    entries: list[_OrderEntry] = []
    with path.open("r", encoding=encoding) as f:
        for line_no, raw_line in enumerate(f, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                decoded = json.loads(line)
            except ValueError as e:
                raise ValueError(f"Invalid JSON at {path}:{line_no}: {e}") from e

            if not isinstance(decoded, dict):
                raise ValueError(
                    f"Invalid order entry at {path}:{line_no}: expected object, got {decoded!r}"
                )

            raw_id = decoded.get("id")
            if not isinstance(raw_id, str):
                raise ValueError(
                    f"Invalid order entry at {path}:{line_no}: missing/invalid 'id'"
                )
            try:
                record_id = coerce_record_id(raw_id)
            except ValueError as e:
                raise ValueError(
                    f"Invalid record id at {path}:{line_no}: {raw_id!r}"
                ) from e

            raw_created_at = decoded.get("created_at")
            if not isinstance(raw_created_at, str):
                raise ValueError(
                    f"Invalid order entry at {path}:{line_no}: missing/invalid 'created_at'"
                )
            try:
                created_at = datetime.fromisoformat(raw_created_at)
            except ValueError as e:
                raise ValueError(
                    f"Invalid created_at at {path}:{line_no}: {raw_created_at!r}"
                ) from e

            relpath = decoded.get("relpath")
            if not isinstance(relpath, str):
                raise ValueError(
                    f"Invalid order entry at {path}:{line_no}: missing/invalid 'relpath'"
                )

            entries.append(
                _OrderEntry(id_=record_id, created_at=created_at, relpath=relpath)
            )
    return entries


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
        raise ValueError(
            "Datetime comparison failed. Ensure `created_at`, `start`, and `end` "
            "are all either timezone-aware or timezone-naive."
        ) from e

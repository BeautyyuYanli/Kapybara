"""Folder-backed storage for :class:`k.agent.memory.entities.MemoryRecord`.

This store persists one record per file under a root folder.

Layout (relative to `root`):
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
- Store order is the lexicographic order of `MemoryRecord.id_`.
- "Latests" are record ids sorted by descending lexicographic id order and can
  be filtered by `in_channel` subtree prefix.
- Parsing is strict: invalid JSON or invalid `MemoryRecord` data raises
  `ValueError` with path/line context.
- Missing records referenced by parent/child links are treated as deleted
  records: load removes dangling links to them and tries to bridge their
  parent/child neighbors when those neighbors are inferable from existing
  records.
- `MemoryRecord` loading expects channel fields (`in_channel`, optional
  `out_channel`).
- `append()` updates each existing referenced parent's `children` list
  (persisting parent records) before persisting the new record. Missing parent
  ids are dropped from the appended record.
- Cache invalidation is keyed off stat snapshots of record-related files under
  `records/`.
- Datetime ordering/range checks compare normalized POSIX-millisecond keys so
  legacy timezone-aware records and newer timezone-naive records can coexist.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Set
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.messages import BaseToolCallPart, ModelResponse

from k.agent.memory.entities import MemoryRecord, datetime_to_posix_millis
from k.agent.memory.store import (
    MemoryRecordId,
    MemoryRecordRef,
    MemoryStore,
    coerce_record_id,
)


@dataclass(frozen=True, slots=True)
class _CacheKey:
    file_stats: tuple[tuple[str, int, int], ...]


type LineMatch = tuple[int, str]
type FileMatches = list[tuple[Path, list[LineMatch]]]


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


def _dedupe_existing_ids(ids: list[str], *, existing_ids: set[str]) -> list[str]:
    """Return ids in original order, keeping only existing ids and removing dups."""

    out: list[str] = []
    seen: set[str] = set()
    for id_ in ids:
        if id_ in seen or id_ not in existing_ids:
            continue
        seen.add(id_)
        out.append(id_)
    return out


def _is_loadable_record_file(path: Path) -> bool:
    """Return whether `path` is a core/legacy record JSON file."""

    name = path.name
    if name.endswith(".detailed.json"):
        return False
    if name.endswith(".detailed.jsonl"):
        return False
    if name.endswith(".compacted.json"):
        return False
    return name.endswith(".json")


def _is_record_related_file(path: Path) -> bool:
    """Return whether `path` should participate in cache invalidation."""

    name = path.name
    return name.endswith(
        (".core.json", ".detailed.json", ".detailed.jsonl", ".compacted.json")
    ) or _is_loadable_record_file(path)


def _parse_rg_lines_with_numbers(output: str) -> list[tuple[Path, int, str]]:
    """Parse `rg` output lines in `path:line:match` form."""

    parsed: list[tuple[Path, int, str]] = []
    for raw in output.splitlines():
        if not raw:
            continue
        parts = raw.split(":", 2)
        if len(parts) != 3:
            continue
        path_s, line_s, text = parts
        try:
            line_no = int(line_s)
        except ValueError:
            continue
        parsed.append((Path(path_s), line_no, text))
    return parsed


def _in_channel_matches_prefix(record_channel: str, in_channel_prefix: str) -> bool:
    """Return whether `record_channel` belongs to the `in_channel_prefix` subtree."""

    return record_channel == in_channel_prefix or record_channel.startswith(
        in_channel_prefix + "/"
    )


class FolderMemoryStore(MemoryStore):
    """Query and append `MemoryRecord` objects stored in a folder.

    Record order is defined as lexicographic sort by `record.id_`.

    Fast retrieval helpers:
    - `get_latests()` returns ids in newest-first store order, optionally
      filtered by `in_channel` prefix.
    - `filter_by_in_channel()` and `search_by_keywords()` mirror Telegram
      stage_a's no-index lookup strategy and shell out to `rg`.

    Load behavior is self-healing for missing records:
    - Parent/child ids pointing to missing records are removed.
    - When both sides are inferable, existing records on each side are bridged
      directly (`missing.parents -> missing.children`).
    """

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
        """Force a reload from disk (even if cache stat snapshots did not change)."""

        self._cache_key = None
        self._load_if_needed()

    def get_latests(self, *, in_channel: str | None = None) -> list[str]:
        """Return latest record ids in descending store order.

        Args:
            in_channel: Optional channel prefix filter using subtree semantics.
        """

        self._load_if_needed()
        if in_channel is None:
            return [record.id_ for record in reversed(self._records)]

        return [
            record.id_
            for record in reversed(self._records)
            if _in_channel_matches_prefix(record.in_channel, in_channel)
        ]

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
        records.sort(
            key=lambda r: (
                datetime_to_posix_millis(r.created_at),
                order.get(r.id_, 1_000_000_000),
            )
        )
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
        start_key = datetime_to_posix_millis(start)
        end_key = datetime_to_posix_millis(end)
        if start_key > end_key:
            raise ValueError(f"start must be <= end; got start={start!r}, end={end!r}")

        self._load_if_needed()

        indexed: list[tuple[int, str, int]] = []
        for idx, record in enumerate(self._records):
            if _in_datetime_range(
                record.created_at,
                start,
                end,
                include_start=include_start,
                include_end=include_end,
            ):
                indexed.append(
                    (idx, record.id_, datetime_to_posix_millis(record.created_at))
                )

        indexed.sort(key=lambda t: (t[2], t[0]))
        return [record_id for _, record_id, _ in indexed]

    def filter_by_in_channel(
        self,
        *,
        in_channel_prefix: str,
        records_dir: Path | None = None,
    ) -> list[Path]:
        """Return detailed files whose record `in_channel` matches `in_channel_prefix`.

        This is intentionally stage_a-like:
        - It shells out to `rg` for coarse file discovery.
        - It then validates each candidate by parsing its sibling `*.core.json`
          and applying subtree-aware prefix matching.
        - Files are returned in path order.

        Args:
            in_channel_prefix: Channel prefix to match by subtree semantics.
            records_dir: Optional explicit records directory. When omitted,
                `<self.root>/records` is used.
        """

        records_dir = records_dir if records_dir is not None else self._records_dir()
        if not records_dir.exists():
            return []

        root_segment = in_channel_prefix.split("/", 1)[0]
        root_pattern = re.escape(root_segment)
        grep_pattern = rf'"in_channel"\s*:\s*"{root_pattern}(?:/|")'

        try:
            res = subprocess.run(
                [
                    "rg",
                    "-l",
                    "--sort",
                    "path",
                    "-g",
                    "*.core.json",
                    grep_pattern,
                    str(records_dir),
                ],
                capture_output=True,
                text=True,
            )
        except OSError:
            return []

        if res.returncode not in (0, 1):
            return []

        detailed_files: list[Path] = []
        for core_file in (line for line in res.stdout.splitlines() if line):
            core_path = Path(core_file)
            try:
                payload = json.loads(core_path.read_text(encoding=self.encoding))
            except (OSError, ValueError):
                continue

            record_channel = (
                payload.get("in_channel") if isinstance(payload, dict) else None
            )
            if not isinstance(record_channel, str):
                continue
            if not _in_channel_matches_prefix(record_channel, in_channel_prefix):
                continue

            detailed_path = self._detailed_path_for_record_path(core_path)
            if detailed_path.exists():
                detailed_files.append(detailed_path)

        return detailed_files

    def search_by_keywords(
        self,
        *,
        files: list[Path],
        pattern: str,
        n: int,
        first_match_per_file: bool = False,
    ) -> FileMatches:
        """Search `files` with `rg` and return stage_a-style grouped matches.

        Args:
            files: Candidate detailed files to scan.
            pattern: Regex pattern passed directly to `rg`.
            n: Keep only the last `n` files in sorted path order. For each kept
                file, keep at most the last `n` matched lines.
            first_match_per_file: If true, ask `rg` to keep only one match per
                file (`--max-count 1`), mirroring stage_a's `user` route.
        """

        if n <= 0 or not files:
            return []

        args = ["rg", "--with-filename", "--line-number", "--no-heading"]
        if first_match_per_file:
            args.extend(["--max-count", "1"])
        args.append(pattern)
        args.extend(str(path) for path in files)

        try:
            res = subprocess.run(
                args,
                capture_output=True,
                text=True,
            )
        except OSError:
            return []

        if res.returncode not in (0, 1):
            return []

        grouped: dict[Path, list[LineMatch]] = {}
        for path, line_no, text in _parse_rg_lines_with_numbers(res.stdout):
            grouped.setdefault(path, []).append((line_no, text))

        selected_paths = sorted(grouped.keys())[-n:]
        selected: FileMatches = []
        for path in selected_paths:
            matches = grouped[path]
            if first_match_per_file:
                selected.append((path, matches[:1]))
            else:
                selected.append((path, matches[-n:]))
        return selected

    def append(self, record: MemoryRecord) -> None:
        self._load_if_needed()

        if record.id_ in self._by_id:
            raise ValueError(
                f"Duplicate MemoryRecord id encountered while appending: {record.id_}"
            )

        existing_ids = set(self._by_id)
        record.parents = _dedupe_existing_ids(record.parents, existing_ids=existing_ids)

        updated_parents: list[MemoryRecord] = []
        for parent_id in record.parents:
            parent = self._by_id.get(parent_id)
            if parent is None:
                continue
            if record.id_ not in parent.children:
                parent.children.append(record.id_)
                updated_parents.append(parent)

        for parent in updated_parents:
            self._persist_record(parent)

        self._persist_record(record)

        self._records.append(record)
        self._records.sort(key=lambda r: r.id_)
        self._by_id[record.id_] = record
        self._cache_key = self._stat_key()

    def _detailed_path_for_record_path(self, record_path: Path) -> Path:
        """Return the sibling detailed path for a record path.

        The record path may be the canonical `<id>.core.json` file or a legacy
        `<id>.json` file.
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

        key = self._stat_key()
        if self._cache_key is not None and key == self._cache_key:
            return

        records: list[MemoryRecord] = []
        by_id: dict[str, MemoryRecord] = {}
        record_paths: dict[str, Path] = {}
        for record_path in self._list_loadable_record_paths():
            try:
                raw = record_path.read_text(encoding=self.encoding)
            except OSError as e:
                raise ValueError(
                    f"Failed to read MemoryRecord at {record_path}: {e}"
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
                record_paths[record_id] = self._persist_record(by_id[record_id])
            key = self._stat_key()

        self._records = records
        self._by_id = by_id
        self._record_paths = record_paths
        self._cache_key = key

    def _list_loadable_record_paths(self) -> list[Path]:
        records_dir = self._records_dir()
        if not records_dir.exists():
            return []

        paths: list[Path] = []
        for path in records_dir.rglob("*.json"):
            if not _is_loadable_record_file(path):
                continue
            if (
                path.name.endswith(".json")
                and not path.name.endswith(".core.json")
                and (path.with_name(f"{path.stem}.core.json")).exists()
            ):
                # If both legacy "<id>.json" and "<id>.core.json" exist, the core
                # file is authoritative.
                continue
            paths.append(path)
        paths.sort(key=lambda p: str(p.relative_to(self.root)))
        return paths

    def _expected_id_for_record_path(self, path: Path) -> str:
        name = path.name
        if name.endswith(".core.json"):
            raw_id = name[: -len(".core.json")]
        elif name.endswith(".json"):
            raw_id = name[: -len(".json")]
        else:
            raise ValueError(f"Unexpected record filename: {path}")
        try:
            return coerce_record_id(raw_id)
        except ValueError as e:
            raise ValueError(f"Invalid record filename at {path}: {raw_id!r}") from e

    def _stat_key(self) -> _CacheKey | None:
        if not self.root.exists():
            return None

        records_dir = self._records_dir()
        if not records_dir.exists():
            return _CacheKey(file_stats=tuple())

        stats: list[tuple[str, int, int]] = []
        for path in records_dir.rglob("*"):
            if not path.is_file() or not _is_record_related_file(path):
                continue
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            stats.append(
                (
                    str(path.relative_to(self.root)),
                    stat.st_mtime_ns,
                    stat.st_size,
                )
            )
        stats.sort(key=lambda item: item[0])
        return _CacheKey(file_stats=tuple(stats))

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

    def _repair_missing_links(
        self,
        *,
        records: list[MemoryRecord],
        by_id: dict[str, MemoryRecord],
        missing_record_ids: set[str],
    ) -> set[str]:
        """Repair links affected by missing records and return touched record ids."""

        existing_ids = set(by_id)
        missing_ids = set(missing_record_ids)
        for record in records:
            missing_ids.update(id_ for id_ in record.parents if id_ not in existing_ids)
            missing_ids.update(
                id_ for id_ in record.children if id_ not in existing_ids
            )

        if not missing_ids:
            return set()

        # Infer a missing node's parents from `children` pointers and infer its
        # children from `parents` pointers, then connect those neighbors directly.
        missing_to_parents: dict[str, list[str]] = {id_: [] for id_ in missing_ids}
        missing_to_children: dict[str, list[str]] = {id_: [] for id_ in missing_ids}
        for record in records:
            for child_id in record.children:
                if child_id not in missing_ids:
                    continue
                if record.id_ not in missing_to_parents[child_id]:
                    missing_to_parents[child_id].append(record.id_)
            for parent_id in record.parents:
                if parent_id not in missing_ids:
                    continue
                if record.id_ not in missing_to_children[parent_id]:
                    missing_to_children[parent_id].append(record.id_)

        repaired: set[str] = set()
        for missing_id in missing_ids:
            parent_ids = missing_to_parents.get(missing_id, [])
            child_ids = missing_to_children.get(missing_id, [])
            for parent_id in parent_ids:
                parent = by_id[parent_id]
                for child_id in child_ids:
                    if child_id == parent_id:
                        continue
                    child = by_id[child_id]
                    if child_id not in parent.children:
                        parent.children.append(child_id)
                        repaired.add(parent_id)
                    if parent_id not in child.parents:
                        child.parents.append(parent_id)
                        repaired.add(child_id)

        for record in records:
            cleaned_parents = _dedupe_existing_ids(
                record.parents,
                existing_ids=existing_ids,
            )
            if cleaned_parents != record.parents:
                record.parents = cleaned_parents
                repaired.add(record.id_)

            cleaned_children = _dedupe_existing_ids(
                record.children,
                existing_ids=existing_ids,
            )
            if cleaned_children != record.children:
                record.children = cleaned_children
                repaired.add(record.id_)

        return repaired

    def _coerce_record(self, record: MemoryRecordRef) -> MemoryRecord:
        if isinstance(record, MemoryRecord):
            return record
        record_id = coerce_record_id(record)
        rec = self._by_id.get(record_id)
        if rec is None:
            raise KeyError(f"Unknown MemoryRecord id: {record_id}")
        return rec


def _in_datetime_range(
    value: datetime,
    start: datetime,
    end: datetime,
    *,
    include_start: bool,
    include_end: bool,
) -> bool:
    value_key = datetime_to_posix_millis(value)
    start_key = datetime_to_posix_millis(start)
    end_key = datetime_to_posix_millis(end)
    if include_start:
        left_ok = value_key >= start_key
    else:
        left_ok = value_key > start_key
    if include_end:
        right_ok = value_key <= end_key
    else:
        right_ok = value_key < end_key
    return left_ok and right_ok

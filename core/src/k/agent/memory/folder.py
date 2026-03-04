"""Folder-backed storage for :class:`k.agent.memory.entities.MemoryRecord`.

This store persists one record per file under a root folder.

Collaborators:
- `k.agent.memory.folder_store.sidecar`: sidecar-index lifecycle, cache
  invalidation, and auto-refresh drift probing.

Layout (relative to `root`):
- `records/YYYY/MM/DD/HH/<id>.core.json`: one JSON blob per record (one line),
  storing record metadata (`in_channel`, `out_channel`, `contacts`, links) and
  `compacted`.
- `records/YYYY/MM/DD/HH/<id>.detailed.jsonl`: a JSONL file (one JSON value per
  non-empty line). Line 1 is the raw `input` (a JSON string). Line 2 is the
  record `output` (a JSON string). Each subsequent non-empty line corresponds
  to one `ModelResponse` and is a JSON array of simplified tool call parts
  extracted from that response. Each element is an object with only `tool_name`
  and `args`. `ModelRequest` messages and full `ModelResponse` objects are not
  persisted in this detailed file.
- `index/by-id/<prefix>/<id>.json`: per-record filesystem metadata index used
  for scalable lookups (id/path mapping + routing/link metadata).
- `index/order.ids`: lexicographically sorted record ids (one id per line).
- `index/records.epoch`: writer-touched stamp used for cheap cache
  invalidation across multiple `FolderMemoryStore` instances.

Design notes / invariants:
- Store order is the lexicographic order of `MemoryRecord.id_`.
- "Latests" are record ids sorted by descending lexicographic id order and can
  be filtered by `in_channel` subtree prefix, exact contact-id membership, and
  optional `num` limiting.
- Parsing is strict: invalid JSON or invalid `MemoryRecord` data raises
  `ValueError` with path/line context.
- Missing records referenced by parent/child links are treated as deleted
  records: load removes dangling links to them and tries to bridge their
  parent/child neighbors when those neighbors are inferable from existing
  records.
- `MemoryRecord` loading expects channel fields (`in_channel`, optional
  `out_channel`) plus optional `contacts`.
- `append()` updates each existing referenced parent's `children` list
  (persisting parent records) before persisting the new record. Missing parent
  ids are dropped from the appended record.
- Process-local hot-record caches are intentionally small and invalidated by
  `index/records.epoch` checks rather than recursive file stat snapshots.
- A full on-disk rebuild pass is still used to bootstrap/repair the filesystem
  index (including missing-link self-healing) so correctness remains tied to
  the authoritative record files.
- Datetime ordering/range checks compare normalized POSIX-millisecond keys so
  legacy timezone-aware records and newer timezone-naive records can coexist.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Set
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError
from pydantic_ai.messages import BaseToolCallPart, ModelResponse

from k.agent.memory.entities import MemoryRecord, datetime_to_posix_millis
from k.agent.memory.folder_store import FolderSidecarIndexMixin
from k.agent.memory.store import (
    MemoryRecordId,
    MemoryRecordRef,
    MemoryStore,
    coerce_record_id,
)

type LineMatch = tuple[int, str]
type FileMatches = list[tuple[Path, list[LineMatch]]]


class _CoreRecordOnDisk(BaseModel):
    """On-disk schema for `<id>.core.json` in the split core/detailed format."""

    created_at: datetime
    in_channel: str
    out_channel: str | None = None
    contacts: list[str] = Field(default_factory=list)
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
        contacts=list(core.contacts),
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


class FolderMemoryStore(FolderSidecarIndexMixin, MemoryStore):
    """Query and append `MemoryRecord` objects stored in a folder.

    Record order is defined as lexicographic sort by `record.id_` and persisted
    under `index/order.ids` (filesystem text index).

    Fast retrieval helpers:
    - `get_latests()` returns ids in newest-first store order, optionally
      filtered by `in_channel` prefix and exact `contact` membership, with an
      optional `num` cap.
    - `filter_by_in_channel()` remains stage_a-like (`rg` + core validation)
      to keep retrieval tolerant for partially populated core files.
    - `search_by_keywords()` mirrors Telegram stage_a and shells out to `rg`.

    Load behavior is self-healing for missing records:
    - Parent/child ids pointing to missing records are removed.
    - When both sides are inferable, existing records on each side are bridged
      directly (`missing.parents -> missing.children`).

    Runtime cache policy:
    - Only a small LRU cache of hot records/metadata is kept in memory.
    - The canonical source of truth remains on-disk `records/**/*.core.json` +
      `records/**/*.detailed.jsonl`.
    - `refresh()` is explicit, and the sidecar layer also performs a throttled
      drift probe to auto-refresh when out-of-band record-file changes are
      detected.
    """

    root: Path
    encoding: str

    _RECORD_CACHE_LIMIT = 256
    _META_CACHE_LIMIT = 4096
    _AUTO_REFRESH_PROBE_INTERVAL_NS = 5_000_000_000

    def __init__(self, root: str | Path, *, encoding: str = "utf-8") -> None:
        self.root = Path(root)
        self.encoding = encoding
        self._init_sidecar_state()

    def get_latests(
        self,
        *,
        in_channel: str | None = None,
        contact: str | None = None,
        num: int | None = None,
    ) -> list[str]:
        """Return latest record ids in descending store order.

        Args:
            in_channel: Optional channel prefix filter using subtree semantics.
            contact: Optional exact contact-id filter against
                `MemoryRecord.contacts`.
            num: Optional maximum number of ids to return. `None` means no
                limit.
        """

        if num is not None and num < 0:
            raise ValueError(f"num must be >= 0 or None; got {num}")
        if num == 0:
            return []

        self._load_if_needed()
        if not self.root.exists():
            return []

        latests: list[str] = []
        for record_id in self._iter_order_ids_desc():
            meta = self._read_meta(record_id)
            if meta is None:
                continue
            record_channel = meta["in_channel"]
            if not isinstance(record_channel, str):
                raise ValueError(
                    f"Invalid in_channel in metadata for record id {record_id}"
                )
            if in_channel is not None and not _in_channel_matches_prefix(
                record_channel, in_channel
            ):
                continue

            contacts = meta["contacts"]
            if not isinstance(contacts, list) or any(
                not isinstance(v, str) for v in contacts
            ):
                raise ValueError(
                    f"Invalid contacts in metadata for record id {record_id}"
                )
            if contact is not None and contact not in contacts:
                continue

            latests.append(record_id)
            if num is not None and len(latests) >= num:
                break

        return latests

    def get_by_id(self, id_: MemoryRecordId) -> MemoryRecord | None:
        self._load_if_needed()
        if not self.root.exists():
            coerce_record_id(id_)
            return None

        record_id = coerce_record_id(id_)
        return self._get_by_id(record_id, allow_rebuild=True)

    def get_by_ids(
        self, ids: Set[MemoryRecordId], *, strict: bool = False
    ) -> list[MemoryRecord]:
        self._load_if_needed()
        if not self.root.exists():
            record_ids = {coerce_record_id(id_) for id_ in ids}
            if strict and record_ids:
                missing_str = ", ".join(str(i) for i in sorted(record_ids))
                raise KeyError(f"Missing record(s): {missing_str}")
            return []

        record_ids = {coerce_record_id(id_) for id_ in ids}
        missing = [id_ for id_ in record_ids if self._read_meta(id_) is None]
        if strict and missing:
            missing_str = ", ".join(str(i) for i in sorted(missing))
            raise KeyError(f"Missing record(s): {missing_str}")

        records: list[MemoryRecord] = []
        for record_id in record_ids:
            rec = self._get_by_id(record_id, allow_rebuild=True)
            if rec is None:
                continue
            records.append(rec)

        records.sort(
            key=lambda r: (
                datetime_to_posix_millis(r.created_at),
                r.id_,
            )
        )
        return records

    def get_parents(
        self, record: MemoryRecordRef, *, strict: bool = False
    ) -> list[str]:
        self._load_if_needed()
        rec = self._coerce_record(record)
        if strict:
            missing = [id_ for id_ in rec.parents if self._read_meta(id_) is None]
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
            missing = [id_ for id_ in rec.children if self._read_meta(id_) is None]
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
        """Return ancestor ids for `record` following parent links.

        `level=0` returns `[record.id_]` to preserve a convenient identity case
        for callers that cap traversal depth dynamically.
        """

        if level is not None and level < 0:
            raise ValueError(f"level must be >= 0 or None; got {level}")

        self._load_if_needed()
        current = self._coerce_record(record)

        if level == 0:
            return [current.id_]

        def _checked_parents(parent_ids: list[str]) -> list[str]:
            if strict:
                missing = [id_ for id_ in parent_ids if self._read_meta(id_) is None]
                if missing:
                    missing_str = ", ".join(str(i) for i in missing)
                    raise KeyError(f"Missing parent record(s): {missing_str}")
            return parent_ids

        ancestors: list[str] = []
        seen: set[str] = set()

        # Walk links from the filesystem metadata index. Avoid calling
        # `get_parents()` repeatedly inside traversal because that method
        # re-checks cache freshness on every call.
        frontier = _checked_parents(list(current.parents))
        depth = 0
        while frontier and (level is None or depth < level):
            depth += 1
            next_frontier: list[str] = []
            for parent_id in frontier:
                if parent_id in seen:
                    continue
                seen.add(parent_id)
                ancestors.append(parent_id)

                parent_meta = self._read_meta(parent_id)
                if parent_meta is None:
                    if strict:
                        raise KeyError(f"Unknown parent MemoryRecord id: {parent_id}")
                    continue
                parent_parents = self._validate_list_of_strings(
                    record_id=parent_id,
                    field_name="parents",
                    value=parent_meta["parents"],
                )
                next_frontier.extend(_checked_parents(parent_parents))
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
        if not self.root.exists():
            return []

        indexed: list[tuple[int, str]] = []
        for record_id in self._iter_order_ids():
            meta = self._read_meta(record_id)
            if meta is None:
                continue
            created_at_ms = meta["created_at_ms"]
            if not isinstance(created_at_ms, int):
                raise ValueError(
                    f"Invalid created_at_ms in metadata for record id {record_id}"
                )
            if _in_datetime_range_ms(
                created_at_ms,
                start_key,
                end_key,
                include_start=include_start,
                include_end=include_end,
            ):
                indexed.append((created_at_ms, record_id))

        indexed.sort(key=lambda t: (t[0], t[1]))
        return [record_id for _, record_id in indexed]

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

        # Keep this stage_a-like and tolerant: only parse `in_channel` from
        # candidate core files and avoid full-store bootstrap validation.
        return self._filter_by_in_channel_via_rg(
            in_channel_prefix=in_channel_prefix,
            records_dir=records_dir,
        )

    def _filter_by_in_channel_via_rg(
        self,
        *,
        in_channel_prefix: str,
        records_dir: Path,
    ) -> list[Path]:
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
        if not self.root.exists():
            self.root.mkdir(parents=True, exist_ok=True)
        self._load_if_needed()

        if self._read_meta(record.id_) is not None:
            raise ValueError(
                f"Duplicate MemoryRecord id encountered while appending: {record.id_}"
            )

        existing_ids = {
            parent_id
            for parent_id in record.parents
            if self._read_meta(parent_id) is not None
        }
        record.parents = _dedupe_existing_ids(record.parents, existing_ids=existing_ids)

        updated_parents: list[MemoryRecord] = []
        for parent_id in record.parents:
            parent = self._get_by_id(parent_id, allow_rebuild=True)
            if parent is None:
                continue
            if record.id_ not in parent.children:
                parent.children.append(record.id_)
                updated_parents.append(parent)

        for parent in updated_parents:
            parent_path = self._record_paths.get(parent.id_)
            persisted_parent_path = self._persist_record(parent, path_hint=parent_path)
            self._upsert_meta(parent, core_path=persisted_parent_path)
            self._cache_record(parent)

        persisted_path = self._persist_record(record, path_hint=None)
        self._upsert_meta(record, core_path=persisted_path)
        self._cache_record(record)
        self._insert_id_into_order(record.id_)
        self._touch_epoch()
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

    def _load_record_from_record_path(self, record_path: Path) -> MemoryRecord:
        try:
            raw = record_path.read_text(encoding=self.encoding)
        except OSError as e:
            raise ValueError(
                f"Failed to read MemoryRecord at {record_path}: {e}"
            ) from e

        record = _load_memory_record_from_disk(
            record_path,
            raw,
            encoding=self.encoding,
            detailed_path=self._detailed_path_for_record_path(record_path),
        )
        expected_id = self._expected_id_for_record_path(record_path)
        if record.id_ != expected_id:
            raise ValueError(
                f"Record id mismatch at {record_path}: expected {expected_id}, got {record.id_}"
            )
        return record

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

    def _persist_record(self, record: MemoryRecord, *, path_hint: Path | None) -> Path:
        path = (
            path_hint if path_hint is not None else self._record_paths.get(record.id_)
        )
        if path is None or not path.name.endswith(".core.json"):
            path = self._record_path_for(record)

        # Split persistence:
        # - core: metadata + channel routing + compacted (one JSON blob, one line)
        # - detailed: raw input + output + tool_calls per response (JSONL)
        self._atomic_write_text(
            path,
            record.dump_compated(),
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
        rec = self._get_by_id(record_id, allow_rebuild=True)
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


def _in_datetime_range_ms(
    value_ms: int,
    start_ms: int,
    end_ms: int,
    *,
    include_start: bool,
    include_end: bool,
) -> bool:
    if include_start:
        left_ok = value_ms >= start_ms
    else:
        left_ok = value_ms > start_ms
    if include_end:
        right_ok = value_ms <= end_ms
    else:
        right_ok = value_ms < end_ms
    return left_ok and right_ok

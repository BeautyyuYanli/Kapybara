"""Folder-backed storage for :class:`k.agent.memory.entities.MemoryRecord`.

This store persists one record per file under a root folder.

Collaborators:
- `k.agent.memory.entities`: `MemoryRecord` validation plus id/datetime helpers.
- `k.agent.memory.store`: query/append protocol consumed by agents.

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
- Optional legacy `<id>.compacted.json` sidecars may exist next to legacy
  `<id>.json` records and remain readable for backward compatibility.

Design notes / invariants:
- The canonical source of truth is always `records/**/*.core.json` plus
  `records/**/*.detailed.jsonl`. No derived `index/` tree is maintained.
- Store order is the lexicographic order of `MemoryRecord.id_`.
- Queries read the filesystem directly and only parse what they need:
  `get_latests()` without filters can answer from filenames, metadata-only
  queries read only `*.core.json`, and full record loads read the sibling
  detailed file.
- Parsing is strict: invalid JSON or invalid `MemoryRecord` data raises
  `ValueError` with path/line context.
- Missing records referenced by parent/child links are treated as deleted.
  Record loads repair the visible graph on the fly by dropping dangling links
  and bridging a single missing hop when existing neighbors reveal both sides.
- `append()` updates each existing referenced parent's `children` list
  (persisting parent records) before persisting the new record. Missing parent
  ids are dropped from the appended record.
- Canonical record writes share one advisory lock under the store root so
  cross-process append operations never overlap.
- New records publish `*.core.json` last so disk scans never observe a visible
  record before its required `*.detailed.jsonl` payload exists.
- `refresh()` is retained for `MemoryStore` compatibility but is a no-op:
  disk-backed queries already observe external filesystem changes directly.
- Datetime ordering/range checks compare normalized POSIX-millisecond keys so
  legacy timezone-aware records and newer timezone-naive records can coexist.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Iterator, Set
from contextlib import contextmanager
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


@dataclass(frozen=True, slots=True)
class _RecordHeader:
    """Metadata that can be answered from a record's core JSON alone."""

    id_: str
    created_at_ms: int
    in_channel: str
    contacts: tuple[str, ...]
    parents: tuple[str, ...]
    children: tuple[str, ...]
    path: Path


@dataclass(slots=True)
class _ProcessStoreLockState:
    """Per-process bookkeeping for a root-scoped advisory store lock."""

    mutex: threading.RLock
    fd: int | None = None
    depth: int = 0


@dataclass(frozen=True, slots=True)
class _LinkSnapshot:
    """Ephemeral record graph used to repair missing on-disk links."""

    headers_by_id: dict[str, _RecordHeader]
    missing_to_parents: dict[str, list[str]]
    missing_to_children: dict[str, list[str]]


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


def _detailed_path_for_record_path(record_path: Path) -> Path:
    """Return the sibling detailed path for a record path."""

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
            raise ValueError(f"Invalid MemoryRecord JSON at {record_path}: {e}") from e

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

    try:
        core = _CoreRecordOnDisk.model_validate(decoded)
    except ValidationError as e:
        raise ValueError(f"Invalid MemoryRecord JSON at {record_path}: {e}") from e

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


def _load_record_header_from_disk(record_path: Path, *, encoding: str) -> _RecordHeader:
    """Load metadata required for queries without touching the detailed payload."""

    try:
        raw = record_path.read_text(encoding=encoding)
    except OSError as e:
        raise ValueError(f"Failed to read MemoryRecord at {record_path}: {e}") from e

    try:
        decoded = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"Invalid JSON at {record_path}: {e}") from e

    if not isinstance(decoded, dict):
        raise ValueError(f"Invalid MemoryRecord JSON at {record_path}: expected object")

    if "input" in decoded:
        try:
            record = MemoryRecord.model_validate(decoded)
        except ValidationError as e:
            raise ValueError(f"Invalid MemoryRecord JSON at {record_path}: {e}") from e
        return _RecordHeader(
            id_=record.id_,
            created_at_ms=datetime_to_posix_millis(record.created_at),
            in_channel=record.in_channel,
            contacts=tuple(record.contacts),
            parents=tuple(record.parents),
            children=tuple(record.children),
            path=record_path,
        )

    try:
        core = _CoreRecordOnDisk.model_validate(decoded)
    except ValidationError as e:
        raise ValueError(f"Invalid MemoryRecord JSON at {record_path}: {e}") from e

    detailed_path = _detailed_path_for_record_path(record_path)
    if not detailed_path.exists():
        raise ValueError(f"Missing detailed file for id {core.id_}: {detailed_path}")

    return _RecordHeader(
        id_=core.id_,
        created_at_ms=datetime_to_posix_millis(core.created_at),
        in_channel=core.in_channel,
        contacts=tuple(core.contacts),
        parents=tuple(core.parents),
        children=tuple(core.children),
        path=record_path,
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


def _dedupe_ids_preserving_order(ids: list[str]) -> list[str]:
    """Return `ids` without duplicates while preserving the first occurrence."""

    out: list[str] = []
    seen: set[str] = set()
    for id_ in ids:
        if id_ in seen:
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

    Record order is defined as lexicographic sort by `record.id_` and derived
    directly from the record filenames on disk.

    Fast retrieval helpers:
    - `contains_id()` checks existence from filenames alone, avoiding a full
      record load plus link repair during polling/orchestration paths.
    - `get_latests()` returns ids in newest-first store order, optionally
      filtered by `in_channel` prefix and exact `contact` membership, with an
      optional `num` cap.
    - `filter_by_in_channel()` remains stage_a-like (`rg` + core validation)
      to keep retrieval tolerant for partially populated core files.
    - `search_by_keywords()` mirrors Telegram stage_a and shells out to `rg`.

    Load behavior is self-healing for missing records:
    - Parent/child ids pointing to missing records are dropped from the visible
      record returned by `get_by_id()`.
    - When both sides are inferable, existing records on each side are bridged
      directly (`missing.parents -> missing.children`) in the returned view.
    """

    root: Path
    encoding: str

    def __init__(self, root: str | Path, *, encoding: str = "utf-8") -> None:
        self.root = Path(root)
        self.encoding = encoding

    def refresh(self) -> None:
        """Retained for `MemoryStore` compatibility; direct disk queries need no rebuild."""

        return None

    def contains_id(self, id_: MemoryRecordId) -> bool:
        """Return whether `id_` exists without loading or repairing the record."""

        if not self.root.exists():
            coerce_record_id(id_)
            return False

        record_id = coerce_record_id(id_)
        return self._find_record_path_by_id(record_id) is not None

    def _store_lock_path(self) -> Path:
        return self.root / ".folder-memory-store.lock"

    @contextmanager
    def _exclusive_store_lock(self) -> Iterator[None]:
        """Serialize cross-process canonical writes under the store root."""

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
        if not self.root.exists():
            return []

        record_paths = self._list_loadable_record_paths()
        if not record_paths:
            return []

        if in_channel is None and contact is None:
            ids = self._sorted_record_ids_from_paths(record_paths)
            return ids if num is None else ids[:num]

        headers_by_id = self._scan_record_headers_by_id(record_paths=record_paths)
        latests: list[str] = []
        for record_id in sorted(headers_by_id, reverse=True):
            header = headers_by_id[record_id]
            if in_channel is not None and not _in_channel_matches_prefix(
                header.in_channel, in_channel
            ):
                continue
            if contact is not None and contact not in header.contacts:
                continue
            latests.append(record_id)
            if num is not None and len(latests) >= num:
                break

        return latests

    def get_by_id(self, id_: MemoryRecordId) -> MemoryRecord | None:
        if not self.root.exists():
            coerce_record_id(id_)
            return None

        record_id = coerce_record_id(id_)
        return self._get_by_id(record_id)

    def get_by_ids(
        self, ids: Set[MemoryRecordId], *, strict: bool = False
    ) -> list[MemoryRecord]:
        record_ids = {coerce_record_id(id_) for id_ in ids}
        if not self.root.exists():
            if strict and record_ids:
                missing_str = ", ".join(str(i) for i in sorted(record_ids))
                raise KeyError(f"Missing record(s): {missing_str}")
            return []

        records: list[MemoryRecord] = []
        missing: list[str] = []
        for record_id in record_ids:
            rec = self._get_by_id(record_id)
            if rec is None:
                missing.append(record_id)
                continue
            records.append(rec)

        if strict and missing:
            missing_str = ", ".join(str(i) for i in sorted(missing))
            raise KeyError(f"Missing record(s): {missing_str}")

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
        rec = self._coerce_record(record)
        if strict:
            self._ensure_records_exist(rec.parents, link_name="parent")
        return list(rec.parents)

    def get_children(
        self, record: MemoryRecordRef, *, strict: bool = False
    ) -> list[str]:
        rec = self._coerce_record(record)
        if strict:
            self._ensure_records_exist(rec.children, link_name="child")
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

        current = self._coerce_record(record)
        if level == 0:
            return [current.id_]

        frontier = list(current.parents)
        if strict:
            self._ensure_records_exist(frontier, link_name="parent")

        ancestors: list[str] = []
        seen: set[str] = set()
        depth = 0
        while frontier and (level is None or depth < level):
            depth += 1
            next_frontier: list[str] = []
            for parent_id in frontier:
                if parent_id in seen:
                    continue
                seen.add(parent_id)
                ancestors.append(parent_id)

                parent = self._get_by_id(parent_id)
                if parent is None:
                    if strict:
                        raise KeyError(f"Unknown parent MemoryRecord id: {parent_id}")
                    continue
                if strict:
                    self._ensure_records_exist(parent.parents, link_name="parent")
                next_frontier.extend(parent.parents)
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
        if not self.root.exists():
            return []

        indexed: list[tuple[int, str]] = []
        for header in self._scan_record_headers_by_id().values():
            if _in_datetime_range_ms(
                header.created_at_ms,
                start_key,
                end_key,
                include_start=include_start,
                include_end=include_end,
            ):
                indexed.append((header.created_at_ms, header.id_))

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
        # candidate core files and avoid full-store validation.
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
        """Persist `record` and serialize against concurrent appends."""

        with self._exclusive_store_lock():
            if self._find_record_path_by_id(record.id_) is not None:
                raise ValueError(
                    f"Duplicate MemoryRecord id encountered while appending: {record.id_}"
                )

            updated_parents: list[MemoryRecord] = []
            resolved_parent_ids: list[str] = []
            for parent_id in _dedupe_ids_preserving_order(record.parents):
                parent = self._get_by_id(parent_id)
                if parent is None:
                    continue
                resolved_parent_ids.append(parent_id)
                if record.id_ not in parent.children:
                    parent.children.append(record.id_)
                    updated_parents.append(parent)

            record.parents = resolved_parent_ids

            for parent in updated_parents:
                parent_path = self._find_record_path_by_id(parent.id_)
                if parent_path is None:
                    continue
                self._persist_record(parent, path_hint=parent_path)

            self._persist_record(record, path_hint=None)

    def _get_by_id(self, record_id: str) -> MemoryRecord | None:
        record_path = self._find_record_path_by_id(record_id)
        if record_path is None:
            return None

        record = self._load_record_from_record_path(record_path)
        return self._repair_record_links(record)

    def _repair_record_links(self, record: MemoryRecord) -> MemoryRecord:
        if not record.parents and not record.children:
            return record

        snapshot = self._load_link_snapshot()
        resolved_parents = self._resolve_parent_ids(
            list(record.parents),
            snapshot=snapshot,
        )
        resolved_children = self._resolve_child_ids(
            list(record.children),
            snapshot=snapshot,
        )
        if resolved_parents == list(record.parents) and resolved_children == list(
            record.children
        ):
            return record
        return record.model_copy(
            update={
                "parents": resolved_parents,
                "children": resolved_children,
            }
        )

    def _load_link_snapshot(self) -> _LinkSnapshot:
        headers_by_id = self._scan_record_headers_by_id()
        existing_ids = set(headers_by_id)
        missing_to_parents: dict[str, list[str]] = {}
        missing_to_children: dict[str, list[str]] = {}

        for header in headers_by_id.values():
            for child_id in header.children:
                if child_id in existing_ids:
                    continue
                missing_to_parents.setdefault(child_id, [])
                if header.id_ not in missing_to_parents[child_id]:
                    missing_to_parents[child_id].append(header.id_)

            for parent_id in header.parents:
                if parent_id in existing_ids:
                    continue
                missing_to_children.setdefault(parent_id, [])
                if header.id_ not in missing_to_children[parent_id]:
                    missing_to_children[parent_id].append(header.id_)

        return _LinkSnapshot(
            headers_by_id=headers_by_id,
            missing_to_parents=missing_to_parents,
            missing_to_children=missing_to_children,
        )

    def _resolve_parent_ids(
        self,
        parent_ids: list[str],
        *,
        snapshot: _LinkSnapshot,
    ) -> list[str]:
        return self._resolve_link_ids(
            parent_ids,
            snapshot=snapshot,
            bridge_map=snapshot.missing_to_parents,
        )

    def _resolve_child_ids(
        self,
        child_ids: list[str],
        *,
        snapshot: _LinkSnapshot,
    ) -> list[str]:
        return self._resolve_link_ids(
            child_ids,
            snapshot=snapshot,
            bridge_map=snapshot.missing_to_children,
        )

    def _resolve_link_ids(
        self,
        ids: list[str],
        *,
        snapshot: _LinkSnapshot,
        bridge_map: dict[str, list[str]],
    ) -> list[str]:
        resolved: list[str] = []
        seen: set[str] = set()
        for id_ in ids:
            if id_ in snapshot.headers_by_id:
                if id_ not in seen:
                    seen.add(id_)
                    resolved.append(id_)
                continue

            for bridged_id in bridge_map.get(id_, []):
                if bridged_id in seen or bridged_id not in snapshot.headers_by_id:
                    continue
                seen.add(bridged_id)
                resolved.append(bridged_id)

        return resolved

    def _sorted_record_ids_from_paths(self, record_paths: list[Path]) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        record_paths_sorted = sorted(
            record_paths,
            key=lambda path: self._expected_id_for_record_path(path),
            reverse=True,
        )
        for record_path in record_paths_sorted:
            record_id = self._expected_id_for_record_path(record_path)
            if record_id in seen:
                raise ValueError(f"Duplicate MemoryRecord id on disk: {record_id}")
            seen.add(record_id)
            ids.append(record_id)
        return ids

    def _scan_record_headers_by_id(
        self, *, record_paths: list[Path] | None = None
    ) -> dict[str, _RecordHeader]:
        headers: dict[str, _RecordHeader] = {}
        paths = (
            record_paths
            if record_paths is not None
            else self._list_loadable_record_paths()
        )
        for record_path in paths:
            header = _load_record_header_from_disk(record_path, encoding=self.encoding)
            expected_id = self._expected_id_for_record_path(record_path)
            if header.id_ != expected_id:
                raise ValueError(
                    f"Record id mismatch at {record_path}: expected {expected_id}, got {header.id_}"
                )
            if header.id_ in headers:
                existing_path = headers[header.id_].path
                raise ValueError(
                    f"Duplicate MemoryRecord id on disk: {header.id_} ({existing_path}, {record_path})"
                )
            headers[header.id_] = header
        return headers

    def _find_record_path_by_id(self, record_id: str) -> Path | None:
        records_dir = self._records_dir()
        if not records_dir.exists():
            return None

        core_paths = sorted(
            records_dir.glob(f"**/{record_id}.core.json"),
            key=self._relative_path_sort_key,
        )
        if len(core_paths) > 1:
            raise ValueError(f"Duplicate MemoryRecord id on disk: {record_id}")
        if core_paths:
            return core_paths[0]

        legacy_paths = sorted(
            (
                path
                for path in records_dir.glob(f"**/{record_id}.json")
                if _is_loadable_record_file(path)
            ),
            key=self._relative_path_sort_key,
        )
        if len(legacy_paths) > 1:
            raise ValueError(f"Duplicate MemoryRecord id on disk: {record_id}")
        return legacy_paths[0] if legacy_paths else None

    def _ensure_records_exist(self, record_ids: list[str], *, link_name: str) -> None:
        missing = [
            record_id
            for record_id in record_ids
            if self._find_record_path_by_id(record_id) is None
        ]
        if not missing:
            return
        missing_str = ", ".join(str(i) for i in missing)
        raise KeyError(f"Missing {link_name} record(s): {missing_str}")

    def _detailed_path_for_record_path(self, record_path: Path) -> Path:
        return _detailed_path_for_record_path(record_path)

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
                and path.with_name(f"{path.stem}.core.json").exists()
            ):
                # If both legacy "<id>.json" and "<id>.core.json" exist, the core
                # file is authoritative.
                continue
            paths.append(path)
        paths.sort(key=self._relative_path_sort_key)
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

    def _relative_path_sort_key(self, path: Path) -> str:
        return str(path.relative_to(self.root))

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
        path = path_hint if path_hint is not None else self._record_path_for(record)

        # Split persistence:
        # - core: metadata + channel routing + compacted (one JSON blob, one line)
        # - detailed: raw input + output + tool_calls per response (JSONL)
        detailed_path = self._detailed_path_for_record_path(path)
        # Publish the detailed sidecar first and the core file last so a disk
        # scan never sees a visible core record without its required payload.
        self._atomic_write_text(
            detailed_path,
            _encode_detailed_jsonl(record),
        )
        self._atomic_write_text(
            path,
            record.dump_compated(),
        )

        return path

    def _coerce_record(self, record: MemoryRecordRef) -> MemoryRecord:
        if isinstance(record, MemoryRecord):
            return record
        record_id = coerce_record_id(record)
        rec = self._get_by_id(record_id)
        if rec is None:
            raise KeyError(f"Unknown MemoryRecord id: {record_id}")
        return rec


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

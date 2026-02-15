"""Migration helpers for folder-backed memory records.

This module converts legacy record layouts into the split core/detailed format:

- `<id>.core.json`: one JSON object (single line), storing record metadata plus
  `compacted`.
- `<id>.detailed.jsonl`: JSONL (one JSON value per non-empty line).
    - line 1: raw `input` (JSON string)
    - line 2: record `output` (JSON string)
    - line 3: simplified tool calls (JSON array, one line)

Legacy layouts supported:
- `<id>.core.json` containing `input` and `output` (but not `compacted`)
- `<id>.compacted.json` sidecar containing `compacted` (JSON array of strings)

Note: historical stores typically did not persist per-step `ModelResponse`
traces. This migration writes `input` and `output` plus a simplified `tool_calls`
list into `*.detailed.jsonl`. If the legacy record already has a `detailed`
field containing response-like objects, this migration extracts tool calls from
those responses.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = path.parent if path.parent.exists() else None
    with tempfile.NamedTemporaryFile(
        "w",
        encoding=encoding,
        dir=tmp_dir,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tf:
        tmp_path = Path(tf.name)
        tf.write(text)
    tmp_path.replace(path)


def iter_record_files(records_root: Path) -> Iterable[Path]:
    """Yield record file paths under `records_root` (excluding detailed/sidecars)."""

    for path in records_root.rglob("*.json"):
        name = path.name
        if name.endswith((".detailed.json", ".compacted.json")):
            continue
        if (
            name.endswith(".json")
            and not name.endswith(".core.json")
            and path.with_name(f"{path.stem}.core.json").exists()
        ):
            # If both legacy "<id>.json" and "<id>.core.json" exist, migrate the
            # core file only.
            continue
        yield path


def record_id_from_path(path: Path) -> str:
    name = path.name
    if name.endswith(".core.json"):
        return name[: -len(".core.json")]
    if name.endswith(".json"):
        return name[: -len(".json")]
    raise ValueError(f"Unexpected record filename: {path}")


def detailed_path_for_record_path(path: Path) -> Path:
    record_id = record_id_from_path(path)
    return path.with_name(f"{record_id}.detailed.jsonl")


def legacy_detailed_path_for_record_path(path: Path) -> Path:
    record_id = record_id_from_path(path)
    return path.with_name(f"{record_id}.detailed.json")


def core_path_for_record_path(path: Path) -> Path:
    record_id = record_id_from_path(path)
    return path.with_name(f"{record_id}.core.json")


def legacy_compacted_path_for_record_path(path: Path) -> Path:
    record_id = record_id_from_path(path)
    return path.with_name(f"{record_id}.compacted.json")


def coerce_compacted(value: Any) -> list[str] | None:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        return None
    return list(value)


def extract_legacy_model_responses(value: Any) -> list[dict[str, Any]]:
    """Best-effort extraction of ModelResponse-shaped dicts from legacy payloads."""

    if not isinstance(value, list):
        return []
    responses: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict) and item.get("kind") == "response":
            responses.append(item)
    return responses


def _extract_tool_calls_from_response_dict(
    resp: dict[str, Any],
) -> list[dict[str, Any]]:
    parts = resp.get("parts")
    if not isinstance(parts, list):
        return []

    tool_calls: list[dict[str, Any]] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("part_kind") != "tool-call":
            continue

        tool_name = part.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name:
            continue

        args = part.get("args")
        if args is not None and not isinstance(args, (str, dict)):
            continue

        tool_calls.append({"tool_name": tool_name, "args": args})
    return tool_calls


def _extract_tool_calls_from_legacy_detailed(value: Any) -> list[dict[str, Any]]:
    tool_calls: list[dict[str, Any]] = []
    for resp in extract_legacy_model_responses(value):
        tool_calls.extend(_extract_tool_calls_from_response_dict(resp))
    return tool_calls


def _parse_existing_detailed_jsonl(
    text: str,
) -> tuple[str | None, str | None, list[dict[str, Any]]]:
    """Parse an existing `*.detailed.jsonl` into `(input, output, tool_calls)`.

    Supports both:
    - current schema: 3 lines (input, output, tool_calls)
    - prior schema: input+output then N lines of response objects
    """

    non_empty = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(non_empty) < 2:
        return None, None, []

    try:
        decoded_input = json.loads(non_empty[0])
        decoded_output = json.loads(non_empty[1])
    except ValueError:
        return None, None, []

    if not isinstance(decoded_input, str) or not isinstance(decoded_output, str):
        return None, None, []

    if len(non_empty) >= 3:
        try:
            third = json.loads(non_empty[2])
        except ValueError:
            third = None

        if isinstance(third, list):
            tool_calls: list[dict[str, Any]] = []
            for item in third:
                if not isinstance(item, dict):
                    continue
                tool_name = item.get("tool_name")
                if not isinstance(tool_name, str) or not tool_name:
                    continue
                args = item.get("args")
                if args is not None and not isinstance(args, (str, dict)):
                    continue
                tool_calls.append({"tool_name": tool_name, "args": args})
            return decoded_input, decoded_output, tool_calls

        tool_calls: list[dict[str, Any]] = []
        for line in non_empty[2:]:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if isinstance(obj, dict) and obj.get("kind") == "response":
                tool_calls.extend(_extract_tool_calls_from_response_dict(obj))
        return decoded_input, decoded_output, tool_calls

    return decoded_input, decoded_output, []


def migrate_record_file(
    path: Path,
    *,
    delete_legacy_compacted: bool,
    force: bool,
    dry_run: bool,
    encoding: str = "utf-8",
) -> bool:
    """Migrate a single record file; return True if any change would be made."""

    raw = path.read_text(encoding=encoding)
    record = json.loads(raw)
    if not isinstance(record, dict):
        raise ValueError(f"Expected JSON object in record file: {path}")

    core_path = core_path_for_record_path(path)
    detailed_path = detailed_path_for_record_path(path)
    legacy_detailed_path = legacy_detailed_path_for_record_path(path)
    legacy_compacted_path = legacy_compacted_path_for_record_path(path)

    input_value = record.get("input")
    if input_value is not None and not isinstance(input_value, str):
        raise ValueError(f"Invalid 'input' field (expected string) in {path}")

    output_value = record.get("output")
    if output_value is not None and not isinstance(output_value, str):
        raise ValueError(f"Invalid 'output' field (expected string) in {path}")

    compacted: list[str] | None = coerce_compacted(record.get("compacted"))
    if compacted is None and legacy_compacted_path.exists():
        compacted = coerce_compacted(
            json.loads(legacy_compacted_path.read_text(encoding=encoding))
        )
    compacted = compacted or []

    core_payload: dict[str, Any] = {
        k: v for k, v in record.items() if k not in {"input", "output", "detailed"}
    }
    core_payload["compacted"] = compacted

    core_text = (
        json.dumps(core_payload, ensure_ascii=False, separators=(",", ":")) + "\n"
    )

    existing_detailed_text = ""
    if legacy_detailed_path.exists():
        existing_detailed_text = legacy_detailed_path.read_text(encoding=encoding)
    elif detailed_path.exists():
        existing_detailed_text = detailed_path.read_text(encoding=encoding)

    detail_lines: list[str] = []
    if input_value is not None and output_value is not None:
        detail_lines.append(json.dumps(input_value, ensure_ascii=False))
        detail_lines.append(json.dumps(output_value, ensure_ascii=False))
        tool_calls = _extract_tool_calls_from_legacy_detailed(record.get("detailed"))
        detail_lines.append(
            json.dumps(tool_calls, ensure_ascii=False, separators=(",", ":"))
        )
    elif existing_detailed_text:
        parsed_input, parsed_output, parsed_tool_calls = _parse_existing_detailed_jsonl(
            existing_detailed_text
        )
        if parsed_input is not None and parsed_output is not None:
            detail_lines = [
                json.dumps(parsed_input, ensure_ascii=False),
                json.dumps(parsed_output, ensure_ascii=False),
                json.dumps(
                    parsed_tool_calls, ensure_ascii=False, separators=(",", ":")
                ),
            ]
    detailed_text = ("\n".join(detail_lines) + "\n") if detail_lines else ""

    changed = False
    if core_path.exists():
        if core_path.read_text(encoding=encoding) != core_text:
            changed = True
    else:
        changed = True

    if detailed_text:
        if detailed_path.exists():
            if detailed_path.read_text(encoding=encoding) != detailed_text:
                changed = True
        else:
            changed = True

    if dry_run or not changed:
        return changed

    atomic_write_text(core_path, core_text, encoding=encoding)
    if detailed_text:
        atomic_write_text(detailed_path, detailed_text, encoding=encoding)
        if legacy_detailed_path.exists():
            legacy_detailed_path.unlink()

    if delete_legacy_compacted and legacy_compacted_path.exists():
        legacy_compacted_path.unlink()

    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Migrate folder memory records to split core/detailed files."
    )
    ap.add_argument(
        "--records-root",
        default=os.path.expanduser("~/memories/records"),
        help="Root directory containing `YYYY/MM/DD/HH/*.json` records (default: ~/memories/records).",
    )
    ap.add_argument(
        "--delete-legacy-compacted",
        action="store_true",
        help="Delete legacy `*.compacted.json` sidecars after merging them into core records.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Rewrite core/detailed files even if they look already migrated.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing any files.",
    )
    args = ap.parse_args(argv)

    records_root = Path(args.records_root)
    if not records_root.exists():
        raise SystemExit(f"records root does not exist: {records_root}")

    changed = 0
    skipped = 0
    errors = 0

    for path in sorted(iter_record_files(records_root)):
        try:
            did_change = migrate_record_file(
                path,
                delete_legacy_compacted=args.delete_legacy_compacted,
                force=args.force,
                dry_run=args.dry_run,
            )
        except Exception as e:
            errors += 1
            print(f"[error] {path}: {type(e).__name__}: {e}")
            continue

        if did_change:
            changed += 1
            if args.dry_run:
                record_id = record_id_from_path(path)
                print(f"[dry-run] migrate {record_id} ({path})")
        else:
            skipped += 1

    print(f"Done. changed={changed} skipped={skipped} errors={errors}")
    return 1 if errors else 0

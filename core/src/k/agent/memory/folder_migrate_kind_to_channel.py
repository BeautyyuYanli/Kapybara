"""Migrate FolderMemoryStore record files from `kind` to channel fields.

This utility rewrites record JSON files under `root/records/**` so legacy
`kind` fields become:

- `in_channel=<kind>`
- `out_channel=null` (when not already present)

It is intentionally scoped to FolderMemoryStore on-disk files:
- `<id>.core.json`
- legacy `<id>.json`

It skips sidecar/detail files (`*.detailed.jsonl`, `*.detailed.json`,
`*.compacted.json`).

Telegram legacy-note:
- For records with `kind="telegram"`, the migration tries to infer
  `in_channel=telegram/chat/<chat_id>` from the stored input payload.
  Legacy split records store that payload in the first JSON value of
  `<id>.detailed.jsonl`.
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from k.agent.channels import validate_channel_path
from k.starters.telegram.compact import extract_chat_id


@dataclass(slots=True)
class MigrationReport:
    """Summary of one migration run."""

    scanned_files: int = 0
    changed_files: int = 0
    unchanged_files: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def has_errors(self) -> bool:
        return bool(self.errors)


def migrate_folder_memory_kind_to_channel(
    root: str | Path,
    *,
    encoding: str = "utf-8",
    dry_run: bool = True,
) -> MigrationReport:
    """Migrate FolderMemoryStore record files under `root`.

    Args:
        root: FolderMemoryStore root (contains `records/` and `order.jsonl`).
        encoding: File encoding used for reads/writes.
        dry_run: If true, only report what would change.

    Returns:
        A migration report with counts and any per-file errors.
    """

    report = MigrationReport()
    records_root = Path(root).expanduser().resolve() / "records"
    if not records_root.exists():
        return report

    for path in _iter_record_json_files(records_root):
        report.scanned_files += 1
        try:
            raw = path.read_text(encoding=encoding)
            decoded = json.loads(raw)
            changed, migrated = _migrate_record_payload(
                decoded,
                path=path,
                encoding=encoding,
            )
            if changed:
                report.changed_files += 1
                if not dry_run:
                    payload = json.dumps(
                        migrated,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                    _atomic_write_text(path=path, text=payload, encoding=encoding)
            else:
                report.unchanged_files += 1
        except Exception as e:
            report.errors.append(f"{path}: {type(e).__name__}: {e}")

    return report


def _iter_record_json_files(records_root: Path) -> list[Path]:
    files: list[Path] = []
    for path in records_root.rglob("*.json"):
        name = path.name
        if name.endswith(".detailed.json"):
            continue
        if name.endswith(".compacted.json"):
            continue
        files.append(path)
    return sorted(files)


def _detailed_path_for_record(path: Path) -> Path | None:
    name = path.name
    if name.endswith(".core.json"):
        record_id = name.removesuffix(".core.json")
    elif name.endswith(".json") and not name.endswith(".detailed.json"):
        record_id = name.removesuffix(".json")
    else:
        return None
    return path.with_name(f"{record_id}.detailed.jsonl")


def _read_legacy_input_text_from_detailed(path: Path, *, encoding: str) -> str | None:
    """Read the first JSON value (raw input) from a detailed JSONL sidecar.

    The split store format writes the first detailed line as a JSON string.
    For robustness we also accept a JSON object/array line and treat the raw line
    itself as input text.
    """

    try:
        lines = path.read_text(encoding=encoding).splitlines()
    except OSError:
        return None

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        try:
            decoded = json.loads(line)
        except ValueError:
            return None
        if isinstance(decoded, str):
            return decoded
        if isinstance(decoded, (dict, list)):
            return line
        return None
    return None


def _extract_legacy_input_text(
    *,
    data: dict[str, object],
    path: Path,
    encoding: str,
) -> str | None:
    raw_input = data.get("input")
    if isinstance(raw_input, str):
        return raw_input

    detailed_path = _detailed_path_for_record(path)
    if detailed_path is None or not detailed_path.exists():
        return None
    return _read_legacy_input_text_from_detailed(detailed_path, encoding=encoding)


def _parse_telegram_updates(input_text: str) -> list[dict[str, Any]]:
    """Parse one or many Telegram update JSON objects from stored input text."""

    updates: list[dict[str, Any]] = []
    for raw_line in input_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            decoded = json.loads(line)
        except ValueError:
            continue
        if isinstance(decoded, dict):
            updates.append(cast(dict[str, Any], decoded))
    if updates:
        return updates

    stripped = input_text.strip()
    if not stripped:
        return []
    try:
        decoded = json.loads(stripped)
    except ValueError:
        return []

    if isinstance(decoded, dict):
        return [cast(dict[str, Any], decoded)]
    if isinstance(decoded, list):
        return [
            cast(dict[str, Any], item) for item in decoded if isinstance(item, dict)
        ]
    return []


def _infer_in_channel_from_legacy_kind(
    *,
    data: dict[str, object],
    path: Path,
    encoding: str,
) -> str | None:
    """Best-effort channel inference for legacy `kind` payloads.

    Today this is only specialized for Telegram because historic records often
    used `kind="telegram"` while keeping the concrete `chat.id` in input JSON.
    """

    legacy_kind = data.get("kind")
    if legacy_kind != "telegram":
        return None

    input_text = _extract_legacy_input_text(data=data, path=path, encoding=encoding)
    if not input_text:
        return None

    updates = _parse_telegram_updates(input_text)
    if not updates:
        return None

    chat_ids = {
        chat_id
        for update in updates
        if (chat_id := extract_chat_id(update)) is not None
    }
    if len(chat_ids) != 1:
        # Keep chat-level unknown when history contains mixed/no chat ids.
        return "telegram"

    return f"telegram/chat/{next(iter(chat_ids))}"


def _migrate_record_payload(
    payload: object,
    *,
    path: Path,
    encoding: str,
) -> tuple[bool, dict[str, object]]:
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object, got: {type(payload).__name__}")

    data = dict(payload)
    changed = False

    in_channel = data.get("in_channel")
    if isinstance(in_channel, str):
        in_channel = validate_channel_path(in_channel, field_name="in_channel")
    else:
        legacy_kind = data.get("kind")
        if not isinstance(legacy_kind, str):
            raise ValueError("Missing both 'in_channel' and legacy 'kind'")
        inferred = _infer_in_channel_from_legacy_kind(
            data=data,
            path=path,
            encoding=encoding,
        )
        if inferred is not None:
            legacy_kind = inferred
        in_channel = validate_channel_path(
            legacy_kind,
            field_name=f"{path.name}.kind",
        )
        data["in_channel"] = in_channel
        changed = True

    out_channel = data.get("out_channel")
    if out_channel is None:
        # Explicitly store null to document "same as in_channel".
        if "out_channel" not in data:
            data["out_channel"] = None
            changed = True
    elif isinstance(out_channel, str):
        out_channel = validate_channel_path(out_channel, field_name="out_channel")
        if out_channel == in_channel:
            data["out_channel"] = None
            changed = True
    else:
        raise ValueError(f"Invalid out_channel type: {type(out_channel).__name__}")

    if "kind" in data:
        data.pop("kind")
        changed = True

    return changed, data


def _atomic_write_text(*, path: Path, text: str, encoding: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding=encoding,
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tf:
        tmp_path = Path(tf.name)
        tf.write(text)
    tmp_path.replace(path)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Migrate FolderMemoryStore record files from legacy `kind` to "
            "`in_channel`/`out_channel`."
        )
    )
    parser.add_argument(
        "--root",
        default="~/memories",
        help="FolderMemoryStore root directory (default: ~/memories).",
    )
    parser.add_argument(
        "--encoding",
        default="utf-8",
        help="File encoding (default: utf-8).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes to disk. Without this flag the run is dry-run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = migrate_folder_memory_kind_to_channel(
        args.root,
        encoding=args.encoding,
        dry_run=not args.apply,
    )

    mode = "dry-run" if not args.apply else "apply"
    print(
        "\n".join(
            [
                f"Mode: {mode}",
                f"Scanned: {report.scanned_files}",
                f"Changed: {report.changed_files}",
                f"Unchanged: {report.unchanged_files}",
                f"Errors: {len(report.errors)}",
            ]
        )
    )
    for err in report.errors:
        print(f"- {err}")

    return 1 if report.has_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

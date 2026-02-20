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
"""

from __future__ import annotations

import argparse
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from k.agent.channels import validate_channel_path


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
            changed, migrated = _migrate_record_payload(decoded, path=path)
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


def _migrate_record_payload(
    payload: object,
    *,
    path: Path,
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

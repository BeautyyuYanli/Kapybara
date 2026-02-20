#!/usr/bin/env python3
"""Edit a file by replacing a known slice of lines.

This script is used as a "skill" implementation and is also exercised by
`core/tests/test_edit_file_skill.py`.
It is intended to live at `~/.kapybara/skills/meta/edit-file/edit.py`.

Contract:
- `--start-line` is 1-based (like editors). If provided, `old_content` must match
  the file's content starting at that line (after normalizing newline style) or
  the edit fails with exit code 3.
- If `--start-line` is omitted, `old_content` must be non-empty and must match
  exactly once in the file (after normalizing newline style). If it matches
  zero or multiple times, the edit fails with exit code 3.
- On success, the file is rewritten preserving the original file's newline
  style. Trailing-newline presence is preserved unless the edit touches EOF, in
  which case it follows the replacement content (with a special-case for
  deleting the tail, where the last kept line's newline becomes trailing).

Output:
  Prints a human-readable message to stdout and exits with:
    - 0 on success
    - 2 on usage/input errors
    - 3 on edit mismatch / ambiguous match
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _detect_newline_style(raw: bytes) -> str:
    if b"\r\n" in raw:
        return "\r\n"
    if b"\r" in raw:
        return "\r"
    return "\n"


@dataclass(frozen=True, slots=True)
class EditResult:
    ok: bool
    filename: str
    start_line: int
    old_line_count: int
    new_line_count: int
    message: str


class EditMismatchError(RuntimeError):
    pass


def _find_unique_start_line(*, file_text_norm: str, old_norm: str) -> int:
    if old_norm == "":
        raise ValueError("old_content must be non-empty when start_line is omitted.")

    # Find occurrences of the raw normalized substring.
    occurrences: list[int] = []
    start = 0
    while True:
        idx = file_text_norm.find(old_norm, start)
        if idx < 0:
            break
        occurrences.append(idx)
        start = idx + 1

    if not occurrences:
        raise EditMismatchError("old_content did not match anywhere in the file.")
    if len(occurrences) > 1:
        raise EditMismatchError(
            f"old_content matched multiple times ({len(occurrences)} occurrences)."
        )

    unique_idx = occurrences[0]
    # Convert byte offset to 1-based line number in normalized text.
    start_line = file_text_norm.count("\n", 0, unique_idx) + 1
    return start_line


def apply_edit(
    *,
    filename: str,
    start_line: int | None,
    old_content: str,
    new_content: str,
) -> EditResult:
    path = Path(filename)

    raw = path.read_bytes()
    newline_style = _detect_newline_style(raw)
    try:
        file_text = raw.decode("utf-8")
    except UnicodeDecodeError:
        file_text = raw.decode("utf-8-sig")

    file_text_norm = _normalize_newlines(file_text)
    old_norm = _normalize_newlines(old_content)
    new_norm = _normalize_newlines(new_content)

    if start_line is None:
        start_line = _find_unique_start_line(file_text_norm=file_text_norm, old_norm=old_norm)

    if start_line < 1:
        raise ValueError("start_line must be >= 1 (1-based line numbers).")

    if file_text_norm == "":
        file_lines: list[str] = []
        file_ends_with_newline = False
    else:
        file_lines = file_text_norm.split("\n")
        file_ends_with_newline = file_text_norm.endswith("\n")
        if file_ends_with_newline and file_lines and file_lines[-1] == "":
            file_lines = file_lines[:-1]

    old_has_trailing_newline = old_norm != "" and old_norm.endswith("\n")
    new_has_trailing_newline = new_norm != "" and new_norm.endswith("\n")

    old_lines = old_norm.split("\n") if old_norm != "" else []
    if old_has_trailing_newline and old_lines and old_lines[-1] == "":
        old_lines = old_lines[:-1]

    new_lines = new_norm.split("\n") if new_norm != "" else []
    if new_has_trailing_newline and new_lines and new_lines[-1] == "":
        new_lines = new_lines[:-1]

    start_idx = start_line - 1
    if start_idx > len(file_lines):
        raise ValueError(
            f"start_line {start_line} is beyond EOF (file has {len(file_lines)} lines)."
        )

    end_idx = start_idx + len(old_lines)
    if end_idx > len(file_lines):
        raise EditMismatchError(
            f"old_content expects {len(old_lines)} lines from start_line {start_line}, "
            f"but file has only {len(file_lines) - start_idx} lines remaining."
        )

    existing_slice = file_lines[start_idx:end_idx]
    if existing_slice != old_lines:
        raise EditMismatchError("old_content mismatch at start_line.")

    edit_touches_eof = end_idx == len(file_lines)
    updated_lines = file_lines[:start_idx] + new_lines + file_lines[end_idx:]

    if edit_touches_eof:
        if new_lines:
            updated_ends_with_newline = new_has_trailing_newline
        else:
            updated_ends_with_newline = start_idx > 0
    else:
        updated_ends_with_newline = file_ends_with_newline

    updated_norm = "\n".join(updated_lines)
    if updated_ends_with_newline:
        updated_norm += "\n"

    updated = updated_norm.replace("\n", newline_style)
    path.write_text(updated, encoding="utf-8", newline="")

    return EditResult(
        ok=True,
        filename=str(path),
        start_line=start_line,
        old_line_count=len(old_lines),
        new_line_count=len(new_lines),
        message="updated",
    )


def _parse_args(argv: list[str]) -> tuple[str, int | None, str, str]:
    parser = argparse.ArgumentParser(prog="edit.py", exit_on_error=False)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--start-line", type=int, required=False)
    parser.add_argument("--old-content", required=True)
    parser.add_argument("--new-content", required=True)
    args = parser.parse_args(argv)

    return (
        str(args.filename),
        int(args.start_line) if args.start_line is not None else None,
        str(args.old_content),
        str(args.new_content),
    )


def _format_ok(result: EditResult) -> str:
    return (
        f"OK: {result.message}\n"
        f"file: {result.filename}\n"
        f"start_line: {result.start_line}\n"
        f"old_lines: {result.old_line_count}\n"
        f"new_lines: {result.new_line_count}"
    )


def _format_error(*, kind: str, message: str) -> str:
    if "\n" in message:
        return f"ERROR ({kind}):\n{message}"
    return f"ERROR ({kind}): {message}"


def main(argv: list[str]) -> int:
    try:
        filename, start_line, old_content, new_content = _parse_args(argv)
    except Exception as e:
        print(_format_error(kind="usage", message=str(e)))
        return 2

    try:
        res = apply_edit(
            filename=filename,
            start_line=start_line,
            old_content=old_content,
            new_content=new_content,
        )
        print(_format_ok(res))
        return 0
    except EditMismatchError as e:
        print(_format_error(kind="mismatch", message=str(e)))
        return 3
    except Exception as e:
        print(_format_error(kind="error", message=str(e)))
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

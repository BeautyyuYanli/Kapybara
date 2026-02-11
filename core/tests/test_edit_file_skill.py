import subprocess
import sys
from pathlib import Path


def _skill_edit_script() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "data" / "fs" / "skills" / "edit" / "edit.py"


def _run_edit(
    *,
    filename: Path,
    start_line: int,
    old_content: str,
    new_content: str,
) -> subprocess.CompletedProcess[str]:
    script = _skill_edit_script()
    return subprocess.run(
        [
            sys.executable,
            str(script),
            "--filename",
            str(filename),
            "--start-line",
            str(start_line),
            "--old-content",
            old_content,
            "--new-content",
            new_content,
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_edit_replaces_expected_slice(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    target.write_text("one\ntwo\nthree\n", encoding="utf-8", newline="\n")

    proc = _run_edit(
        filename=target,
        start_line=2,
        old_content="two\n",
        new_content="TWO\n",
    )
    assert proc.returncode == 0
    assert proc.stdout.splitlines()[0].startswith("OK:")

    assert target.read_text(encoding="utf-8", newline="") == "one\nTWO\nthree\n"


def test_edit_preserves_crlf_and_trailing_newline(tmp_path: Path) -> None:
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"one\r\ntwo\r\n")

    proc = _run_edit(
        filename=target,
        start_line=2,
        old_content="two\n",
        new_content="TWO\n",
    )
    assert proc.returncode == 0
    assert target.read_bytes() == b"one\r\nTWO\r\n"


def test_edit_mismatch_returns_exit_3(tmp_path: Path) -> None:
    target = tmp_path / "m.txt"
    target.write_text("a\nb\n", encoding="utf-8", newline="\n")

    proc = _run_edit(
        filename=target,
        start_line=2,
        old_content="c\n",
        new_content="B\n",
    )
    assert proc.returncode == 3
    assert proc.stdout.splitlines()[0].startswith("ERROR (mismatch):")
    assert "mismatch" in proc.stdout.lower()


def test_edit_inserts_when_old_content_empty(tmp_path: Path) -> None:
    target = tmp_path / "ins.txt"
    target.write_text("b\n", encoding="utf-8", newline="\n")

    proc = _run_edit(
        filename=target,
        start_line=1,
        old_content="",
        new_content="a\n",
    )
    assert proc.returncode == 0
    assert target.read_text(encoding="utf-8", newline="") == "a\nb\n"


def test_edit_empty_file_treated_as_zero_lines(tmp_path: Path) -> None:
    target = tmp_path / "empty.txt"
    target.write_text("", encoding="utf-8", newline="\n")

    proc = _run_edit(
        filename=target,
        start_line=1,
        old_content="",
        new_content="hello\n",
    )
    assert proc.returncode == 0
    assert target.read_text(encoding="utf-8", newline="") == "hello\n"


def test_edit_script_exists() -> None:
    assert _skill_edit_script().is_file()

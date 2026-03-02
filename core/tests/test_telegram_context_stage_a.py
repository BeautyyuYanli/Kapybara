import json
import os
import subprocess
from pathlib import Path


def _stage_a_script_path() -> Path:
    repo_root = Path(__file__).resolve().parents[2]
    return (
        repo_root
        / "data"
        / "fs"
        / ".kapybara"
        / "skills"
        / "meta"
        / "retrieve-memory"
        / "stage_a"
    )


def _write_record(
    *,
    root: Path,
    bucket: str,
    record_id: str,
    in_channel: str,
    from_id: int,
    update_id: int,
) -> None:
    record_dir = root / bucket
    record_dir.mkdir(parents=True, exist_ok=True)

    core_path = record_dir / f"{record_id}.core.json"
    detailed_path = record_dir / f"{record_id}.detailed.jsonl"

    core_payload = {"id_": record_id, "in_channel": in_channel}
    core_path.write_text(json.dumps(core_payload), encoding="utf-8", newline="\n")

    raw_update = {
        "update_id": update_id,
        "message": {
            "from": {"id": from_id},
            "chat": {"id": -1001},
            "text": record_id,
        },
    }
    # detailed.jsonl line 1 stores a JSON string payload.
    detailed_path.write_text(
        json.dumps(json.dumps(raw_update, ensure_ascii=False), ensure_ascii=False)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _run_stage_a(
    *,
    home: Path,
    in_channel: str,
    kw: str,
    out_path: Path,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        str(_stage_a_script_path()),
        "--in-channel",
        in_channel,
        "--kw",
        kw,
        "--n",
        "20",
        "--out",
        str(out_path),
    ]
    env = os.environ.copy()
    env["HOME"] = str(home)
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )


def _parse_rows(out_text: str) -> dict[str, list[dict[str, object]]]:
    header = "# id\tcore_json\tmatched_detailed_lines"
    lines = out_text.splitlines()
    start = lines.index(header) + 1
    parsed: dict[str, list[dict[str, object]]] = {}
    for line in lines[start:]:
        if not line.strip():
            continue
        record_id, _core_json, matched_s = line.split("\t", 2)
        parsed[record_id] = json.loads(matched_s)
    return parsed


def test_stage_a_script_exists() -> None:
    assert _stage_a_script_path().is_file()


def test_stage_a_keyword_search_is_scoped_to_in_channel_prefix(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    records_root = home / ".kapybara" / "memories" / "records"

    _write_record(
        root=records_root,
        bucket="2026/02/20/00",
        record_id="aaa",
        in_channel="telegram/chat/-1001/thread/10",
        from_id=567113516,
        update_id=1,
    )
    _write_record(
        root=records_root,
        bucket="2026/02/20/00",
        record_id="bbb",
        in_channel="telegram/chat/-1001/thread/11",
        from_id=567113516,
        update_id=2,
    )
    _write_record(
        root=records_root,
        bucket="2026/02/20/00",
        record_id="ccc",
        in_channel="telegram/chat/-1001/thread/10",
        from_id=999999999,
        update_id=3,
    )

    out_path = tmp_path / "stage_a.tsv"
    proc = _run_stage_a(
        home=home,
        in_channel="telegram/chat/-1001/thread/10",
        kw="aaa|bbb|ccc",
        out_path=out_path,
    )
    assert proc.returncode == 0, proc.stderr

    rows = _parse_rows(out_path.read_text(encoding="utf-8"))
    assert set(rows.keys()) == {"aaa", "ccc"}
    assert all(rows[row_id] for row_id in rows)


def test_stage_a_default_root_uses_home_kapybara_memories_records(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    records_root = home / ".kapybara" / "memories" / "records"

    _write_record(
        root=records_root,
        bucket="2026/02/20/00",
        record_id="aaa",
        in_channel="telegram/chat/-1001/thread/10",
        from_id=567113516,
        update_id=1,
    )

    out_path = tmp_path / "stage_a.tsv"
    proc = _run_stage_a(
        home=home,
        in_channel="telegram/chat/-1001/thread/10",
        kw="aaa",
        out_path=out_path,
    )
    assert proc.returncode == 0, proc.stderr

    rows = _parse_rows(out_path.read_text(encoding="utf-8"))
    assert set(rows.keys()) == {"aaa"}


def test_stage_a_does_not_emit_preference_content(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    records_root = home / ".kapybara" / "memories" / "records"
    records_root.mkdir(parents=True)

    preferences_root = home / ".kapybara" / "preferences"
    preferences_root.mkdir(parents=True)
    (preferences_root / "telegram.md").write_text(
        "platform preference", encoding="utf-8", newline="\n"
    )
    (preferences_root / "telegram" / "PREFERENCES.md").parent.mkdir(parents=True)
    (preferences_root / "telegram" / "PREFERENCES.md").write_text(
        "platform nested preference", encoding="utf-8", newline="\n"
    )
    (preferences_root / "contacts" / "telegram" / "567113516.md").parent.mkdir(
        parents=True
    )
    (preferences_root / "contacts" / "telegram" / "567113516.md").write_text(
        "contact preference", encoding="utf-8", newline="\n"
    )

    out_path = tmp_path / "stage_a.tsv"
    proc = _run_stage_a(
        home=home,
        in_channel="telegram/chat/-1001/thread/10",
        kw="anything",
        out_path=out_path,
    )
    assert proc.returncode == 0, proc.stderr

    output = out_path.read_text(encoding="utf-8")
    assert "Preference (telegram.md):" not in output
    assert "Preference (telegram/PREFERENCES.md):" not in output
    assert "User-specific Preference (from_id: 567113516):" not in output
    assert "contact preference" not in output

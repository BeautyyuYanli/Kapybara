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
        / "context"
        / "telegram"
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
    records_root: Path,
    in_channel: str,
    from_id: int,
    out_path: Path,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        str(_stage_a_script_path()),
        "--in-channel",
        in_channel,
        "--from-id",
        str(from_id),
        "--root",
        str(records_root),
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


def _parse_routes(out_text: str) -> dict[str, set[str]]:
    header = "# id\troutes\tcore_json\tmatched_detailed_lines"
    lines = out_text.splitlines()
    start = lines.index(header) + 1
    parsed: dict[str, set[str]] = {}
    for line in lines[start:]:
        if not line.strip():
            continue
        record_id, routes_s, _core_json, _matched = line.split("\t", 3)
        parsed[record_id] = set(filter(None, routes_s.split(",")))
    return parsed


def test_stage_a_script_exists() -> None:
    assert _stage_a_script_path().is_file()


def test_stage_a_user_route_is_cross_in_channel_for_thread_inputs(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    records_root = tmp_path / "records"

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
        records_root=records_root,
        in_channel="telegram/chat/-1001/thread/10",
        from_id=567113516,
        out_path=out_path,
    )
    assert proc.returncode == 0, proc.stderr

    routes = _parse_routes(out_path.read_text(encoding="utf-8"))
    # In-thread, same user.
    assert routes["aaa"] == {"channel", "user"}
    # Cross-thread, same user: user route should still match.
    assert routes["bbb"] == {"user"}
    # In-thread, different user.
    assert routes["ccc"] == {"channel"}


def test_stage_a_reads_preferences_from_kapybara_preferences(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    records_root = tmp_path / "records"
    records_root.mkdir()

    preferences_root = home / ".kapybara" / "preferences"
    preferences_root.mkdir(parents=True)
    (preferences_root / "telegram.md").write_text(
        "platform preference", encoding="utf-8", newline="\n"
    )
    (preferences_root / "telegram" / "PREFERENCES.md").parent.mkdir(parents=True)
    (preferences_root / "telegram" / "PREFERENCES.md").write_text(
        "platform nested preference", encoding="utf-8", newline="\n"
    )
    (preferences_root / "telegram" / "by_user" / "567113516.md").parent.mkdir(
        parents=True
    )
    (preferences_root / "telegram" / "by_user" / "567113516.md").write_text(
        "by-user preference", encoding="utf-8", newline="\n"
    )

    out_path = tmp_path / "stage_a.tsv"
    proc = _run_stage_a(
        home=home,
        records_root=records_root,
        in_channel="telegram/chat/-1001/thread/10",
        from_id=567113516,
        out_path=out_path,
    )
    assert proc.returncode == 0, proc.stderr

    output = out_path.read_text(encoding="utf-8")
    assert "Preference (telegram.md):" in output
    assert "platform preference" in output
    assert "Preference (telegram/PREFERENCES.md):" in output
    assert "platform nested preference" in output
    assert "User-specific Preference (from_id: 567113516):" in output
    assert "by-user preference" in output

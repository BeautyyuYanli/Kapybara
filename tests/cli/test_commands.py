import json
from uuid import uuid4

from typer.testing import CliRunner

from kapy.cli import commands
from kapy.rpc import RpcError


def configured(monkeypatch):
    caller, target = str(uuid4()), str(uuid4())
    monkeypatch.setenv("KAPY_SESSION_ID", caller)
    monkeypatch.setenv("KAPY_SESSION_TOKEN", "test-session-token")
    monkeypatch.setenv("KAPY_MACHINE_ID", "one")
    calls = []

    async def call(path, method, params, *, auth, timeout=60):  # noqa: ASYNC109
        calls.append((method, params, auth))
        return {"ok": True}

    monkeypatch.setattr(commands, "call_local_proxy", call)
    return caller, target, calls


def test_context_target_does_not_replace_authenticated_caller(monkeypatch):
    caller, target, calls = configured(monkeypatch)
    result = CliRunner().invoke(
        commands.app, ["control", "--session", target, "session", "input", "hello"]
    )
    assert result.exit_code == 0, result.output
    assert calls[0][1]["session_id"] == target
    assert calls[0][2]["session_id"] == caller
    assert calls[0][1]["payload"] == "hello"
    assert "request_id" in calls[0][1]


def test_wait_receipt_and_export_snapshot(monkeypatch, tmp_path):
    _, target, _ = configured(monkeypatch)
    calls = []

    async def proxy(path, method, params, *, auth, timeout=60):  # noqa: ASYNC109
        calls.append(params)
        if method == "session.wait":
            return {"submission": {}, "completion": {"outcome": "completed", "output": "ready"}}
        return {
            "items": [{"text": "first" if params["after"] is None else "second"}],
            "next_cursor": "next",
            "snapshot_cursor": "snapshot",
            "has_more": params["after"] is None,
        }

    monkeypatch.setattr(commands, "call_local_proxy", proxy)
    result = CliRunner().invoke(
        commands.app, ["control", "--session", target, "session", "wait", str(uuid4())]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["completion"]["output"] == "ready"
    output = tmp_path / "history.ndjson"
    result = CliRunner().invoke(
        commands.app, ["control", "--session", target, "history", "export", "--output", str(output)]
    )
    assert result.exit_code == 0, result.output
    assert calls[-1]["snapshot"] == "snapshot" and calls[-1]["after"] == "next"
    assert len(output.read_text().splitlines()) == 2


def test_failed_upload_preserves_identical_archive_for_explicit_retry(monkeypatch, tmp_path):
    configured(monkeypatch)
    source = tmp_path / "example"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: example\ndescription: example\n---\nbody")
    calls = []

    async def fail(path, method, params, *, auth, timeout=60):  # noqa: ASYNC109
        calls.append(params)
        raise RpcError(-32022, "Connection lost")

    monkeypatch.setattr(commands, "call_local_proxy", fail)
    result = CliRunner().invoke(commands.app, ["control", "skill", "upload", str(source)])
    assert result.exit_code != 0
    from pathlib import Path

    archive = Path(calls[0]["archive_path"])
    assert archive.exists()
    original = archive.read_bytes()
    (source / "SKILL.md").write_text("changed")
    result = CliRunner().invoke(
        commands.app,
        [
            "control",
            "skill",
            "upload",
            "--archive",
            str(archive),
            "--request-id",
            calls[0]["request_id"],
        ],
    )
    assert result.exit_code != 0
    assert archive.read_bytes() == original
    assert calls[1] == calls[0]
    archive.unlink()
    archive.parent.rmdir()

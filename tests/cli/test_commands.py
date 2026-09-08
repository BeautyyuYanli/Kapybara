import json
from io import BytesIO
from typing import cast
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


def test_session_capability_wins_over_control_token_and_partial_context_never_elevates(monkeypatch):
    caller, target, calls = configured(monkeypatch)
    monkeypatch.setenv("KAPY_CONTROL_TOKEN", "admin-test")
    result = CliRunner().invoke(commands.app, ["control", "--session", target, "session", "get"])
    assert result.exit_code == 0, result.output
    assert calls[0][2] == {"kind": "session", "session_id": caller, "token": "test-session-token"}
    calls.clear()
    monkeypatch.delenv("KAPY_SESSION_TOKEN")
    result = CliRunner().invoke(commands.app, ["control", "session", "get"])
    assert result.exit_code == 2
    assert calls == []
    monkeypatch.setenv("KAPY_SESSION_TOKEN", "test-session-token")
    monkeypatch.delenv("KAPY_SESSION_ID")
    result = CliRunner().invoke(commands.app, ["control", "--session", target, "session", "get"])
    assert result.exit_code == 2
    assert calls == []


def test_mutation_retry_id_is_visible_before_transport_starts(monkeypatch):
    import sys

    configured(monkeypatch)
    observed = []

    async def lost(path, method, params, *, auth, timeout=60):  # noqa: ASYNC109
        sys.stderr.flush()
        observed.append(
            (params["request_id"], cast(BytesIO, sys.stderr.buffer).getvalue().decode())
        )
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "call_local_proxy", lost)
    result = CliRunner().invoke(commands.app, ["control", "session", "delete"])
    assert result.exit_code != 0
    assert observed[0][0] in observed[0][1]
    assert result.stdout == ""


def test_upload_prints_retry_archive_before_transport_starts(monkeypatch, tmp_path):
    import sys
    from pathlib import Path

    configured(monkeypatch)
    source = tmp_path / "example"
    source.mkdir()
    (source / "SKILL.md").write_text("---\nname: example\ndescription: example\n---\nbody")
    observed = []

    async def lost(path, method, params, *, auth, timeout=60):  # noqa: ASYNC109
        sys.stderr.flush()
        observed.append((params, cast(BytesIO, sys.stderr.buffer).getvalue().decode()))
        raise KeyboardInterrupt

    monkeypatch.setattr(commands, "call_local_proxy", lost)
    result = CliRunner().invoke(commands.app, ["control", "skill", "upload", str(source)])
    assert result.exit_code != 0
    params, stderr = observed[0]
    assert params["request_id"] in stderr and params["archive_path"] in stderr
    retained = Path(params["archive_path"])
    assert retained.exists()
    retained.unlink()
    retained.parent.rmdir()


def test_stdin_file_request_option_and_record_jsonl(monkeypatch, tmp_path):
    configured(monkeypatch)
    calls = []

    async def proxy(path, method, params, *, auth, timeout=60):  # noqa: ASYNC109
        calls.append((method, params))
        if method == "session.output":
            return {
                "items": [{"cursor": "one", "text": "a"}, {"cursor": "two", "text": "b"}],
                "next_cursor": "two",
                "has_more": False,
            }
        if method == "session.wait":
            return {"submission": {}, "completion": {"outcome": "completed"}}
        return {"ok": True}

    monkeypatch.setattr(commands, "call_local_proxy", proxy)
    prompt = "long prompt\n" * 5000
    result = CliRunner().invoke(
        commands.app, ["control", "session", "input", "--stdin"], input=prompt
    )
    assert result.exit_code == 0, result.output
    assert calls[-1][1]["payload"] == prompt
    source = tmp_path / "query.sql"
    source.write_text("SELECT text\nFROM records LIMIT 10")
    result = CliRunner().invoke(
        commands.app, ["control", "history", "query", "--file", str(source)]
    )
    assert result.exit_code == 0, result.output
    assert calls[-1][1]["sql"] == source.read_text()
    receipt = str(uuid4())
    result = CliRunner().invoke(
        commands.app, ["control", "session", "wait", "--request-id", receipt]
    )
    assert result.exit_code == 0, result.output
    assert calls[-1][1]["request_id"] == receipt
    result = CliRunner().invoke(commands.app, ["control", "session", "output"])
    assert result.exit_code == 0, result.output
    assert [json.loads(line) for line in result.stdout.splitlines()] == [
        {"cursor": "one", "text": "a"},
        {"cursor": "two", "text": "b"},
    ]


def test_creation_output_mode_and_removed_custom_reply_address(monkeypatch):
    _, _, calls = configured(monkeypatch)
    cli = CliRunner()
    created = cli.invoke(
        commands.app, ["control", "session", "create", "--output-mode", "reply_to", "task"]
    )
    assert created.exit_code == 0, created.output
    assert calls[0][1]["config"]["output_mode"] == "reply_to"
    assert "waiting_id" not in calls[0][1]
    invalid = cli.invoke(
        commands.app, ["control", "session", "create", "--output-mode", "unknown", "task"]
    )
    assert invalid.exit_code != 0 and len(calls) == 1
    removed = cli.invoke(
        commands.app, ["control", "session", "input", "--waiting-id", str(uuid4()), "task"]
    )
    assert removed.exit_code != 0 and len(calls) == 1

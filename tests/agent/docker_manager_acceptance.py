"""Explicit Docker-only acceptance; launch with run_docker_acceptance.py."""

import base64
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import httpx2
import pytest

from kapy.agent import PayloadRef
from kapy.agent.codec import MessageCodec
from kapy.execution.daemon import MachineService  # type: ignore[missing-import]
from kapy.execution.paths import resolve_paths
from kapy.execution.store import ExecutionStore  # type: ignore[missing-import]
from kapy.rpc import JsonObject
from kapy.rpc import JsonValue as RpcJsonValue
from kapy.skills import extract_skill
from kapy.state import JsonValue

from .test_runner import Context, response, runner  # type: ignore[missing-import]

pytestmark = pytest.mark.skipif(not Path("/.dockerenv").exists(), reason="Docker only")


def archived_parts(ctx: Context) -> list[dict[str, JsonValue]]:
    parts: list[dict[str, JsonValue]] = []
    for write in ctx.writes:
        for message in write.messages:
            message_parts = message.data["parts"]
            assert isinstance(message_parts, list)
            for part in message_parts:
                assert isinstance(part, dict)
                parts.append(part)
    return parts


@pytest.mark.asyncio
async def test_real_manager_patch_and_checkpoint_order(tmp_path: Path) -> None:
    paths = resolve_paths(
        state_dir=tmp_path / "state", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run"
    )
    patches = [
        "*** Begin Patch\n*** Add File: hello.txt\n+original\n*** End Patch\n",
        "*** Begin Patch\n*** Update File: hello.txt\n@@\n-original\n+updated\n*** End Patch\n",
    ]
    tools = [("apply_patch", {"patch": patch}) for patch in patches]
    tools.append(("process_start", {"command": "cat hello.txt", "mode": "stdio"}))
    requests = 0
    dispatched: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    async def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        body = json.loads(request.content)
        if requests:
            expected = f"tool-{requests}"
            # At the actual next provider request, the preceding tool result is durable.
            assert any(
                p.get("tool_call_id") == expected and p["part_kind"] == "tool-return"
                for p in archived_parts(ctx)
            )
            returned = next(m for m in body["messages"] if m.get("tool_call_id") == expected)
            if requests == 3:
                assert "updated" in returned["content"]
        requests += 1
        if requests <= len(tools):
            return response(
                name=tools[requests - 1][0],
                args=tools[requests - 1][1],
                call_id=f"tool-{requests}",
            )
        return response(text="Both edits complete")

    async with (
        ExecutionStore(paths, "machine") as store,
        httpx2.AsyncClient(transport=httpx2.MockTransport(handle), trust_env=False) as client,
    ):
        service = MachineService(store, http_client=client, child_env={"PATH": "/usr/bin:/bin"})
        await service.initialize()

        class Dispatch:
            async def call(
                self,
                machine_id: str,
                method: str,
                params: dict[str, Any],
                *,
                timeout: float = 60.0,  # noqa: ASYNC109
            ) -> Any:
                assert machine_id == "machine" and timeout == 60.0
                assert params["session_id"] == str(ctx.session.id)
                data = await MessageCodec(agent.payload_store, ctx.session.id).load(ctx.state)
                pending = data["pending_tools"][-1]
                assert (pending["name"], pending["args"]) == tools[requests - 1]
                call = next(
                    p
                    for p in archived_parts(ctx)
                    if p.get("tool_call_id") == pending["tool_call_id"]
                    and p["part_kind"] == "tool-call"
                )
                call_args = call["args"]
                assert isinstance(call_args, str)
                assert json.loads(call_args) == pending["args"]
                step = pending["steps"][-1]
                saved = dict(step["params"])
                if "kapy_chunk_payload" in saved:
                    raw = await agent.payload_store.get(
                        ctx.session.id, PayloadRef(**saved.pop("kapy_chunk_payload"))
                    )
                    saved["data_base64"] = base64.b64encode(raw).decode()
                assert step["method"] == method and saved == params
                value = await service.handle(method, params)
                assert isinstance(value, dict)
                dispatched.append((method, params, value))
                return value

        agent = runner(client, Dispatch())  # type: ignore[bad-argument-type]
        ctx = Context(agent.initial_state(instructions="Edit hello.txt", skills=[]))
        await service.handle(
            "session.ensure", {"session_id": str(ctx.session.id), "session_token": "test-token"}
        )
        try:
            result = await agent(ctx)
            assert result.output == "Both edits complete" and requests == 4
            cwd = await store.session_cwd(str(ctx.session.id))
            assert (cwd / "hello.txt").read_text() == "updated\n"
            assert not list((cwd / ".kapy-tools/stdin").iterdir())
            starts = [params for method, params, _ in dispatched if method == "process.start"]
            scripts = [p for p in starts if p["argv"][:3] == ["/bin/sh", "-c", 'exec "$@" < "$0"']]
            assert len(scripts) == 2
            assert all(Path(p["argv"][3]).is_absolute() for p in scripts)
            assert len([p for p in starts if p["argv"][0] == "chmod"]) == 1
            records = await store.process_records(str(ctx.session.id))
            assert records
            for record in records:
                info = record["info"]
                assert isinstance(info, dict)
                assert info["state"] == "exited" and info["exit_code"] == 0
            pushes = [p for m, p, _ in dispatched if m == "file.push"]
            assert len(pushes) >= 3
            finished = {
                p["transfer_id"]: value
                for method, p, value in dispatched
                if method == "file.finish"
            }
            assert all(finished[p["transfer_id"]]["state"] == "complete" for p in pushes)
        finally:
            await service.aclose()


def test_extract_write_failure_does_not_publish_or_touch_existing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("SKILL.md", "---\nname: demo\ndescription: useful\n---\n")
        archive.writestr("later.txt", "later")
    source = tmp_path / "input.zip"
    source.write_bytes(buffer.getvalue())
    existing = tmp_path / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep me")
    real_open = Path.open
    writes = 0

    def fail_second_write(path: Path, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        nonlocal writes
        if mode == "xb":
            writes += 1
            if writes == 2:
                assert list(tmp_path.glob(".kapy-skill-*/SKILL.md"))
                raise OSError("injected disk full after first file")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_second_write)
    with pytest.raises(OSError, match="injected disk full"):
        extract_skill(source, tmp_path / "new")
    assert writes == 2
    assert not (tmp_path / "new").exists() and not list(tmp_path.glob(".kapy-skill-*"))
    with pytest.raises(FileExistsError):
        extract_skill(source, existing)
    assert (existing / "keep.txt").read_text() == "keep me"
    assert list(existing.iterdir()) == [existing / "keep.txt"]


@pytest.mark.asyncio
async def test_process_start_preserves_installed_cli_path(tmp_path: Path) -> None:
    paths = resolve_paths(
        state_dir=tmp_path / "state", data_dir=tmp_path / "data", runtime_dir=tmp_path / "run"
    )
    cli = Path("/app/.venv/bin/kapy")
    requests = 0

    def handle(request: httpx2.Request) -> httpx2.Response:
        nonlocal requests
        requests += 1
        if requests == 1:
            return response(
                name="process_start",
                args={
                    "command": "command -v kapy && kapy --help",
                    "mode": "stdio",
                    "wait_ms": 30_000,
                },
            )
        body = json.loads(request.content)
        returned = next(m for m in body["messages"] if m.get("tool_call_id") == "call1")
        assert str(cli) in returned["content"] and "Usage" in returned["content"]
        return response(text="Installed CLI ran")

    async with (
        ExecutionStore(paths, "machine") as store,
        httpx2.AsyncClient(transport=httpx2.MockTransport(handle), trust_env=False) as client,
    ):
        service = MachineService(
            store,
            http_client=client,
            child_env={"PATH": f"{cli.parent}:/usr/bin:/bin", "PYTHONPATH": "/workspace/src"},
        )
        await service.initialize()

        class Dispatch:
            async def call(
                self,
                machine_id: str,
                method: str,
                params: JsonObject,
                *,
                timeout: float = 60.0,  # noqa: ASYNC109
            ) -> RpcJsonValue:
                assert machine_id == "machine" and method == "process.start"
                value = await service.handle(method, params)
                assert isinstance(value, dict)
                process = value["process"]
                assert isinstance(process, dict)
                assert process["state"] == "exited" and process["exit_code"] == 0
                output = value["output"]
                assert isinstance(output, dict)
                stdout = output["stdout"]
                assert isinstance(stdout, dict)
                encoded = stdout["data_base64"]
                assert isinstance(encoded, str)
                text = base64.b64decode(encoded).decode()
                assert text.splitlines()[0] == str(cli)
                assert "Usage" in text and "kapy" in text
                return value

        agent = runner(client, Dispatch())
        ctx = Context(agent.initial_state(instructions="Check CLI installation", skills=[]))
        try:
            await service.handle(
                "session.ensure", {"session_id": str(ctx.session.id), "session_token": "test-token"}
            )
            result = await agent(ctx)
            assert result.output == "Installed CLI ran" and requests == 2
        finally:
            await service.aclose()

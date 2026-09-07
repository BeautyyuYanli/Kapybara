import asyncio
import base64
import hashlib
import json
from importlib.resources import files
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel, ConfigDict, Field

from kapy.agent import ProcessCommand, ScriptTool
from kapy.agent.machine import MachineTools, Operation
from kapy.agent.runner import Runtime

from .test_runner import Caller, Context, process_update, runner  # type: ignore[missing-import]


class FilesCaller(Caller):
    def __init__(self) -> None:
        super().__init__()
        self.transfers: dict[str, dict[str, Any]] = {}
        self.uploads: dict[str, bytes] = {}
        self.commands: list[list[str]] = []
        self.running = False

    async def call(
        self,
        machine_id: str,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 60.0,  # noqa: ASYNC109
    ) -> Any:
        self.calls.append((machine_id, method, params, timeout))
        if method == "process.start":
            self.commands.append(params["argv"])
            update = process_update(params)
            if params["argv"] == ["uname", "-m"]:
                raw = b"x86_64\n"
                update["output"]["stdout"].update(
                    data_base64=base64.b64encode(raw).decode(), next=len(raw), available=len(raw)
                )
            if self.running and params["argv"][0] == "/bin/sh":
                update["process"]["state"] = "running"
            return update
        if method == "process.wait":
            update = process_update(params)
            update["process"]["state"] = "running" if self.running else "exited"
            return update
        if method == "file.push":
            self.transfers[params["transfer_id"]] = {**params, "data": bytearray()}
            return {**params, "state": "open", "offset": 0}
        if method == "file.chunk":
            transfer = self.transfers[params["transfer_id"]]
            assert len(transfer["data"]) == params["offset"]
            transfer["data"].extend(base64.b64decode(params["data_base64"]))
            return {"next": len(transfer["data"])}
        if method == "file.finish":
            transfer = self.transfers[params["transfer_id"]]
            content = bytes(transfer["data"])
            assert len(content) == transfer["size"]
            assert hashlib.sha256(content).hexdigest() == transfer["sha256"]
            self.uploads[transfer["path"]] = content
            return {"state": "complete", "size": len(content), "sha256": transfer["sha256"]}
        raise AssertionError(method)


class EchoArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str
    repeat: int = Field(ge=1, le=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("running", [False, True])
async def test_script_stdin_is_bytes_and_cleanup_waits_for_termination(running: bool) -> None:
    caller = FilesCaller()
    caller.running = running
    plugin = ScriptTool(
        "echo_script",
        "Run a test script",
        EchoArgs,
        lambda args: ProcessCommand(("python", "-", str(args.repeat)), stdin=args.value.encode()),
    )
    async with httpx2.AsyncClient() as client:
        agent = runner(client, caller, plugins=[plugin])
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        runtime = Runtime(agent, ctx, "test")
        await runtime.initialize()
        tools = MachineTools(runtime, caller, (plugin,))
        value = 'literal $(touch never)\n"single\' quotes"\n'
        result = await tools.execute("echo_script", {"value": value, "repeat": 2}, "script-call")
    execute = next(command for command in caller.commands if command[0] == "/bin/sh")
    assert execute == ["/bin/sh", "-c", 'exec "$@" < "$0"', execute[3], "python", "-", "2"]
    assert execute[3].startswith("/session/.kapy-tools/stdin/")
    assert caller.uploads[execute[3]] == value.encode()
    assert any(command[0] == "rm" for command in caller.commands) is not running
    assert result["process"]["state"] == ("running" if running else "exited")


@pytest.mark.asyncio
async def test_large_transfer_checkpoints_use_small_immutable_chunk_refs() -> None:
    caller = FilesCaller()
    async with httpx2.AsyncClient() as client:
        agent = runner(client, caller)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        runtime = Runtime(agent, ctx, "test")
        await runtime.initialize()
        operation = Operation(runtime, caller, "machine", "large-call", "custom", {})
        content = b"abcdef" * 250_000
        await operation.push("/session/large", content)
    assert caller.uploads["/session/large"] == content
    assert max(len(json.dumps(write.state.data)) for write in ctx.writes) < 50_000
    assert "kapy_chunk_payload" in str(ctx.state.data)


def test_generated_manifest_matches_exact_installed_resource_bytes() -> None:
    resource = files("kapy.agent").joinpath("resources/apply_patch")
    manifest = json.loads(resource.joinpath("manifest.json").read_text())
    for architecture, platform in manifest["platforms"].items():
        for name, metadata in platform["files"].items():
            content = resource.joinpath(architecture, name).read_bytes()
            assert len(content) == metadata["bytes"]
            assert hashlib.sha256(content).hexdigest() == metadata["sha256"]
    source = resource.joinpath("SKILL.md").read_text()
    description = resource.joinpath("description.md").read_text()
    assert "Input type: `FREEFORM`" in source
    assert "Input is a JSON object with a `patch` string field" in description
    assert (
        source[source.index("## Formal grammar") :]
        == description[description.index("## Formal grammar") :]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["push", "pull"])
async def test_cancelled_transfer_begin_aborts_the_original_handle(direction: str) -> None:
    class InterruptedBegin(Caller):
        def __init__(self) -> None:
            super().__init__()
            self.active: set[str] = set()

        async def call(
            self,
            machine_id: str,
            method: str,
            params: dict[str, Any],
            *,
            timeout: float = 60.0,  # noqa: ASYNC109
        ) -> Any:
            self.calls.append((machine_id, method, params, timeout))
            if method == f"file.{direction}":
                self.active.add(params["transfer_id"])
                raise asyncio.CancelledError
            if method == "file.abort":
                assert params["transfer_id"] in self.active
                self.active.remove(params["transfer_id"])
                return {"aborted": True}
            raise AssertionError(method)

    caller = InterruptedBegin()
    async with httpx2.AsyncClient() as client:
        agent = runner(client, caller)
        ctx = Context(agent.initial_state(instructions="", skills=[]))
        runtime = Runtime(agent, ctx, "test")
        await runtime.initialize()
        operation = Operation(runtime, caller, "machine", "begin-cancel", "custom", {})
        with pytest.raises(asyncio.CancelledError):
            if direction == "push":
                await operation.push("/session/file", b"contents")
            else:
                await operation.pull("/session/file", maximum=1024)
    assert not caller.active
    assert [call[1] for call in caller.calls] == [f"file.{direction}", "file.abort"]
    assert caller.calls[0][2]["transfer_id"] == caller.calls[1][2]["transfer_id"]

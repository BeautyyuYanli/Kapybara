"""Agent-facing machine adapters using only the approved process/file RPCs."""

import base64
import codecs
import hashlib
import json
import mimetypes
from dataclasses import asdict
from importlib.resources import files
from pathlib import PurePosixPath
from typing import Any, Literal, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import BinaryContent, ModelRetry, RunContext, Tool, ToolReturn

from kapy.rpc import JsonObject, MachineCaller, RpcDisconnected, RpcError, RpcTimeout

from .payloads import PayloadRef
from .types import ProcessCommand, ScriptTool


class OutcomeUnknown(Exception):
    """An external side effect may have happened and must not be replayed."""


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid")
    machine_id: str | None = None


class Start(Params):
    command: str
    mode: Literal["stdio", "pty"] = "pty"
    cwd: str | None = None
    wait_ms: int = Field(default=1000, ge=0, le=30_000)


class Process(Params):
    process_id: UUID


class Wait(Process):
    cursor: dict[str, int] | None = None
    wait_ms: int = Field(default=1000, ge=0, le=30_000)
    max_bytes: int = Field(default=65_536, ge=1, le=65_536)


class Write(Process):
    input: str


class Resize(Process):
    rows: int = Field(ge=1, le=1000)
    cols: int = Field(ge=1, le=1000)


class Kill(Process):
    wait_ms: int = Field(default=5000, ge=0, le=30_000)


class ListProcesses(Params):
    after: str | None = None
    limit: int = Field(default=50, ge=1, le=100)


class PathParams(Params):
    path: str


class Read(PathParams):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=65_536, ge=1, le=65_536)


class FileWrite(PathParams):
    content: str


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patch: str


BUILTINS: dict[str, tuple[type[Params], str]] = {
    "process_start": (
        Start,
        "Run a shell command. A timeout leaves it running; keep its process ID.",
    ),
    "process_wait": (Wait, "Read more output and observe a process. Cursors count original bytes."),
    "process_write": (Write, "Write terminal input once. Use process_wait to see what happened."),
    "process_resize": (Resize, "Resize a terminal to the given rows and columns."),
    "process_kill": (
        Kill,
        "Stop a process group and tracked descendants, with best-effort cleanup.",
    ),
    "process_list": (ListProcesses, "List this session's processes on a machine."),
    "process_release": (
        Process,
        "Delete a finished process's saved output. This cannot be undone.",
    ),
    "file_read": (Read, "Read a bounded UTF-8 file range. Use offsets for larger files."),
    "file_write": (FileWrite, "Replace a file with UTF-8 text, at most 64 KiB. Parent must exist."),
    "read_media": (
        PathParams,
        "Read a complete media file, at most 20 MiB, for the model to inspect.",
    ),
}


def apply_patch_plugin() -> ScriptTool[Patch]:
    resource = files("kapy.agent").joinpath("resources/apply_patch/description.md")
    return ScriptTool(
        "apply_patch",
        resource.read_text(),
        Patch,
        lambda args: ProcessCommand(("apply_patch",), stdin=args.patch.encode()),
    )


class MachineTools:
    def __init__(
        self, runtime: Any, caller: MachineCaller, plugins: tuple[ScriptTool, ...]
    ) -> None:
        self.runtime = runtime
        self.caller = caller
        self.plugins = {p.name: p for p in plugins}

    def tools(self) -> list[Tool]:
        result = []
        for name, (parameters, description) in BUILTINS.items():
            result.append(self._tool(name, parameters.model_json_schema(), description))
        for name, plugin in self.plugins.items():
            schema = plugin.parameters.model_json_schema()
            schema.setdefault("properties", {})["machine_id"] = {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
            }
            result.append(self._tool(name, schema, plugin.description))
        return result

    def _tool(self, name: str, schema: dict[str, Any], description: str) -> Tool:
        async def invoke(ctx: RunContext, **kwargs: Any) -> Any:
            assert ctx.tool_call_id is not None
            return await self.execute(name, kwargs, ctx.tool_call_id)

        return Tool.from_schema(
            invoke,
            name=name,
            description=description,
            json_schema=schema,
            takes_ctx=True,
            sequential=True,
        )

    async def execute(self, name: str, args: dict[str, Any], call_id: str) -> Any:
        try:
            command = None
            if name in BUILTINS:
                parameters = BUILTINS[name][0].model_validate(args)
                machine = parameters.machine_id
                fields = parameters.model_dump(mode="json", exclude_none=True)
                fields.pop("machine_id", None)
            else:
                plugin = self.plugins[name]
                fields = dict(args)
                machine = fields.pop("machine_id", None)
                command = plugin.render(plugin.parameters.model_validate(fields))
                fields = {}
            if machine is None:
                machine = self.runtime.context.session.default_machine_id
            if not isinstance(machine, str) or not machine:
                raise ValueError("Select a machine_id; this session has no default machine")
            operation = Operation(self.runtime, self.caller, machine, call_id, name, args)
            if name in self.plugins:
                assert command is not None
                if name == "apply_patch":
                    binary = await operation.install_apply_patch()
                    command = ProcessCommand((binary,), stdin=command.stdin, cwd=command.cwd)
                return await operation.script(command)
            if name == "process_start":
                fields["argv"] = ["/bin/sh", "-lc", fields.pop("command")]
                fields["process_id"] = operation.identifier("start")
                result = await operation.rpc("process.start", fields)
                return operation.decode_update(result)
            if name == "process_write":
                data = fields.pop("input").encode()
                if len(data) > 65_536:
                    raise ValueError("Terminal input exceeds 64 KiB")
                fields["data_base64"] = base64.b64encode(data).decode()
            if name.startswith("process_"):
                if name == "process_wait" and "cursor" in fields:
                    cursor = fields["cursor"]
                    if set(cursor) not in ({"pty"}, {"stdout", "stderr"}) or any(
                        isinstance(v, bool) or v < 0 for v in cursor.values()
                    ):
                        raise ValueError("Cursor must have pty or stdout/stderr byte positions")
                result = await operation.rpc(name.replace("_", ".", 1), fields)
                return operation.decode_update(result) if name == "process_wait" else result
            if name == "file_write":
                content = fields["content"].encode()
                if len(content) > 65_536:
                    raise ValueError("File text exceeds 64 KiB")
                await operation.push(fields["path"], content)
                return {
                    "path": fields["path"],
                    "bytes": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            if name == "file_read":
                data, size = await operation.pull(
                    fields["path"], offset=fields["offset"], limit=fields["limit"]
                )
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError:
                    text = (
                        "[This range is binary or splits a UTF-8 character; choose another range.]"
                    )
                return {
                    "path": fields["path"],
                    "offset": fields["offset"],
                    "next_offset": fields["offset"] + len(data),
                    "size": size,
                    "eof": fields["offset"] + len(data) >= size,
                    "text": text,
                }
            data, _ = await operation.pull(
                fields["path"], maximum=self.runtime.config.media_max_bytes
            )
            media_type = mimetypes.guess_type(fields["path"])[0]
            if not media_type or not (
                media_type.startswith(("image/", "audio/", "video/"))
                or media_type == "application/pdf"
            ):
                return "Unrecognized media type; use file_read or a machine command to inspect it."
            metadata = {
                "machine_id": machine,
                "path": fields["path"],
                "media_type": media_type,
                "sha256": hashlib.sha256(data).hexdigest(),
                "tool_call_id": call_id,
            }
            return ToolReturn(
                return_value=[
                    f"Media from {fields['path']}",
                    BinaryContent(data, media_type=media_type),
                ],
                metadata=metadata,
            )
        except (ValueError, PermissionError) as exc:
            raise ModelRetry(str(exc)) from exc
        except (RpcError, RpcDisconnected, RpcTimeout, OutcomeUnknown) as exc:
            return {
                "error": "outcome_unknown"
                if isinstance(exc, (RpcDisconnected, RpcTimeout, OutcomeUnknown))
                else "machine_error",
                "message": str(exc)[:2000],
                "tool_call_id": call_id,
            }


class Operation:
    def __init__(
        self,
        runtime: Any,
        caller: MachineCaller,
        machine: str,
        call_id: str,
        name: str,
        args: dict[str, Any],
    ) -> None:
        self.runtime, self.caller, self.machine = runtime, caller, machine
        self.call_id, self.index = call_id, 0
        pending = runtime.data["pending_tools"]
        self.record: dict[str, Any] | None = next(
            (p for p in pending if p["tool_call_id"] == call_id), None
        )
        if self.record is None:
            self.record = {
                "tool_call_id": call_id,
                "name": name,
                "args": args,
                "machine_id": machine,
                "steps": [],
            }
            pending.append(self.record)

    def identifier(self, step: str) -> str:
        return str(
            uuid5(
                self.runtime.context.session.id,
                f"{self.runtime.context.run_id}:{self.call_id}:{step}",
            )
        )

    async def rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        params = {"session_id": str(self.runtime.context.session.id), **params}
        saved_params = await self.externalize(params)
        assert self.record is not None
        steps: list[dict[str, Any]] = self.record["steps"]
        index = self.index
        self.index += 1
        if index < len(steps):
            step = steps[index]
            if step["method"] != method or step["params"] != saved_params:
                raise OutcomeUnknown(
                    "Recovered operation parameters differ; no command was replayed"
                )
            if "result" in step:
                return await self.hydrate(step["result"])
            result = await self.observe(method, params)
        else:
            step = {"method": method, "params": saved_params}
            steps.append(step)
            await self.runtime.checkpoint()
            try:
                result = await self.caller.call(
                    self.machine, method, cast(JsonObject, params), timeout=60.0
                )
            except RpcDisconnected, RpcTimeout:
                result = await self.observe(method, params)
        if not isinstance(result, dict):
            raise ValueError("Machine returned an invalid result")
        step["result"] = await self.externalize(result)
        await self.runtime.checkpoint()
        return result

    async def observe(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            if method.startswith("process.") and "process_id" in params:
                result = await self.caller.call(
                    self.machine,
                    "process.wait",
                    {
                        "session_id": params["session_id"],
                        "process_id": params["process_id"],
                        "wait_ms": 0,
                    },
                    timeout=60.0,
                )
                if method not in ("process.start", "process.wait"):
                    raise OutcomeUnknown(
                        "Process observed; interrupted operation outcome is unknown"
                    )
            elif "transfer_id" in params:
                result = await self.caller.call(
                    self.machine,
                    "file.finish",
                    {
                        "session_id": params["session_id"],
                        "transfer_id": params["transfer_id"],
                        "wait_ms": 0,
                    },
                    timeout=60.0,
                )
                if method not in ("file.push", "file.pull", "file.finish"):
                    raise OutcomeUnknown("Transfer observed; chunk delivery outcome is unknown")
            else:
                raise OutcomeUnknown("Interrupted operation was not replayed")
        except (RpcError, RpcDisconnected, RpcTimeout) as exc:
            raise OutcomeUnknown("Could not recover the known process or transfer handle") from exc
        if not isinstance(result, dict):
            raise OutcomeUnknown("Invalid observation of an interrupted operation")
        return result

    async def externalize(self, value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value)
        if isinstance(result.get("data_base64"), str) and len(result["data_base64"]) > 4096:
            data = base64.b64decode(result.pop("data_base64"), validate=True)
            ref = await self.runtime.codec.store.put(self.runtime.context.session.id, data)
            result["kapy_chunk_payload"] = asdict(ref)
        # Process output carries chunks one level below output/stream.
        if "output" in result:
            result["output"] = {
                key: await self.externalize(item) if isinstance(item, dict) else item
                for key, item in result["output"].items()
            }
        return result

    async def hydrate(self, value: dict[str, Any]) -> dict[str, Any]:
        result = dict(value)
        if "kapy_chunk_payload" in result:
            ref = PayloadRef(**result.pop("kapy_chunk_payload"))
            data = await self.runtime.codec.store.get(self.runtime.context.session.id, ref)
            result["data_base64"] = base64.b64encode(data).decode()
        if "output" in result:
            result["output"] = {
                key: await self.hydrate(item) if isinstance(item, dict) else item
                for key, item in result["output"].items()
            }
        return result

    def decode_update(self, result: dict[str, Any]) -> dict[str, Any]:
        output = result.get("output", {})
        process_id = result["process"]["process_id"]
        decoded = {"kind": output.get("kind")}
        for stream in ("pty", "stdout", "stderr"):
            if stream not in output:
                continue
            chunk = output[stream]
            raw = base64.b64decode(chunk["data_base64"], validate=True)
            if len(raw) > 65_536 or chunk["next"] != chunk["start"] + len(raw):
                raise ValueError("Invalid process output chunk")
            key = f"{self.machine}:{process_id}:{stream}"
            states = self.runtime.data.setdefault("decoders", {})
            previous = states.get(key, {})
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            if previous.get("cursor") == chunk["start"] and not chunk["truncated"]:
                decoder.setstate((base64.b64decode(previous.get("pending", "")), 0))
            text = decoder.decode(raw, final=chunk["eof"])
            states[key] = {
                "cursor": chunk["next"],
                "pending": base64.b64encode(decoder.getstate()[0]).decode(),
            }
            decoded[stream] = {
                **{k: v for k, v in chunk.items() if k != "data_base64"},
                "text": text[:16_384],
                "display_truncated": len(text) > 16_384,
                "reference": {
                    "machine_id": self.machine,
                    "session_id": str(self.runtime.context.session.id),
                    "process_id": process_id,
                    "stream": stream,
                    "cursor": chunk["start"],
                },
            }
        return {**result, "output": decoded}

    async def push(self, path: str, data: bytes) -> None:
        transfer_id = self.identifier(f"push:{self.index}:{path}")
        await self.rpc(
            "file.push",
            {
                "transfer_id": transfer_id,
                "path": path,
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "transport": {"kind": "websocket"},
            },
        )
        try:
            for offset in range(0, len(data), 65_536):
                chunk = data[offset : offset + 65_536]
                result = await self.rpc(
                    "file.chunk",
                    {
                        "transfer_id": transfer_id,
                        "offset": offset,
                        "data_base64": base64.b64encode(chunk).decode(),
                    },
                )
                if result.get("next") != offset + len(chunk):
                    raise ValueError("Push offset mismatch")
            final = await self.rpc("file.finish", {"transfer_id": transfer_id})
            if (
                final.get("state") != "complete"
                or final.get("size") != len(data)
                or (final.get("sha256") != hashlib.sha256(data).hexdigest())
            ):
                raise ValueError("File transfer did not complete with matching size/hash")
        except BaseException:
            await self.abort(transfer_id)
            raise

    async def pull(
        self, path: str, *, offset: int = 0, limit: int | None = None, maximum: int | None = None
    ) -> tuple[bytes, int]:
        transfer_id = self.identifier(f"pull:{self.index}:{path}")
        info = await self.rpc(
            "file.pull",
            {"transfer_id": transfer_id, "path": path, "transport": {"kind": "websocket"}},
        )
        try:
            size = info["size"]
            if maximum is not None and size > maximum:
                raise ValueError(f"Media exceeds the {maximum}-byte limit")
            if offset > size:
                raise ValueError("Read offset is beyond end of file")
            end = min(size, offset + limit) if limit is not None else size
            content = bytearray()
            while offset < end:
                chunk = await self.rpc(
                    "file.chunk",
                    {
                        "transfer_id": transfer_id,
                        "offset": offset,
                        "max_bytes": min(65_536, end - offset),
                    },
                )
                data = base64.b64decode(chunk["data_base64"], validate=True)
                if (
                    not data
                    or len(data) > min(65_536, end - offset)
                    or (
                        chunk["start"] != offset
                        or chunk["next"] != offset + len(data)
                        or chunk["available"] != size
                    )
                ):
                    raise ValueError("File transfer returned a short or inconsistent chunk")
                content.extend(data)
                offset += len(data)
            final = await self.rpc("file.finish", {"transfer_id": transfer_id})
            if final.get("state") != "complete" or final.get("size") != size:
                raise ValueError("File changed or transfer did not complete")
            return bytes(content), size
        except BaseException:
            await self.abort(transfer_id)
            raise

    async def abort(self, transfer_id: str) -> None:
        try:
            await self.caller.call(
                self.machine,
                "file.abort",
                {"session_id": str(self.runtime.context.session.id), "transfer_id": transfer_id},
                timeout=60.0,
            )
        except RpcError, RpcDisconnected, RpcTimeout:
            pass

    async def command(self, argv: tuple[str, ...], *, cwd: str | None = None) -> dict[str, Any]:
        process_id = self.identifier(f"command:{self.index}")
        params: dict[str, Any] = {
            "process_id": process_id,
            "mode": "stdio",
            "argv": list(argv),
            "wait_ms": 1000,
        }
        if cwd is not None:
            params["cwd"] = cwd
        update = await self.rpc("process.start", params)
        # Bounded observation; a running command is returned with its handle.
        if update["process"]["state"] in ("starting", "running", "killing"):
            update = await self.rpc("process.wait", {"process_id": process_id, "wait_ms": 30_000})
        return update

    @staticmethod
    def finished(update: dict[str, Any]) -> bool:
        return update["process"]["state"] in ("exited", "killed", "failed")

    async def preparation(self, argv: tuple[str, ...]) -> dict[str, Any]:
        update = await self.command(argv)
        if not self.finished(update) or update["process"]["exit_code"] != 0:
            raise OutcomeUnknown(
                "Preparation did not finish successfully; its process was retained"
            )
        return update

    async def workspace(self) -> str:
        update = await self.preparation(("pwd",))
        cwd = update["process"]["cwd"]
        if not PurePosixPath(cwd).is_absolute():
            raise ValueError("Machine returned a non-absolute working directory")
        return cwd

    async def script(self, command: ProcessCommand) -> dict[str, Any]:
        argv = command.argv
        stdin_path = None
        if command.stdin is not None:
            cwd = await self.workspace()
            directory = f"{cwd}/.kapy-tools/stdin"
            await self.preparation(("mkdir", "-p", "--", directory))
            stdin_path = f"{directory}/{self.identifier('stdin')}"
            await self.push(stdin_path, command.stdin)
            argv = ("/bin/sh", "-c", 'exec "$@" < "$0"', stdin_path, *argv)
        update = await self.command(argv, cwd=command.cwd)
        result = self.decode_update(update)
        if stdin_path is not None and self.finished(update):
            await self.preparation(("rm", "-f", "--", stdin_path))
        return result

    async def install_apply_patch(self) -> str:
        workspace = await self.workspace()
        update = await self.preparation(("uname", "-m"))
        arch = base64.b64decode(update["output"]["stdout"]["data_base64"]).decode().strip()
        resources = files("kapy.agent").joinpath("resources/apply_patch")
        manifest = json.loads(resources.joinpath("manifest.json").read_text())
        if arch not in manifest["platforms"]:
            raise ValueError(f"apply_patch has no verified binary for {arch}")
        platform = manifest["platforms"][arch]
        directory = f"{workspace}/.kapy-tools/apply-patch/{platform['bundle_sha256']}"
        key = f"{self.machine}:{platform['bundle_sha256']}"
        assert self.record is not None
        needed = self.record.setdefault(
            "install_needed", key not in self.runtime.data.setdefault("installed_plugins", [])
        )
        if needed:
            await self.preparation(("mkdir", "-p", "--", directory))
            for name, metadata in platform["files"].items():
                data = resources.joinpath(arch, name).read_bytes()
                if (
                    len(data) != metadata["bytes"]
                    or hashlib.sha256(data).hexdigest() != metadata["sha256"]
                ):
                    raise ValueError("Bundled apply_patch resource failed verification")
                await self.push(f"{directory}/{name}", data)
            await self.preparation(("chmod", "700", "--", f"{directory}/apply_patch"))
            if key not in self.runtime.data["installed_plugins"]:
                self.runtime.data["installed_plugins"].append(key)
            await self.runtime.checkpoint()
        return f"{directory}/apply_patch"

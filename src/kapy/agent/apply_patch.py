"""The optional apply_patch script plugin and its verified bundled resources."""

import hashlib
import json
from dataclasses import replace
from importlib.resources import files

from pydantic import BaseModel, ConfigDict

from kapy.rpc import JsonObject

from .types import ProcessCommand, ScriptHost, ScriptTool


class Patch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    patch: str


def output_text(update: JsonObject) -> str:
    output = update["output"]
    if isinstance(output, dict) and isinstance(stdout := output.get("stdout"), dict):
        if isinstance(text := stdout.get("text"), str):
            return text
    raise ValueError("Preparation command did not return stdout text")


async def prepare(host: ScriptHost, command: ProcessCommand) -> ProcessCommand:
    workspace = await host.workspace()
    arch = output_text(await host.run(("uname", "-m"))).strip()
    resources = files("kapy.agent").joinpath("resources/apply_patch")
    manifest = json.loads(resources.joinpath("manifest.json").read_text())
    if arch not in manifest["platforms"]:
        raise ValueError(f"apply_patch has no verified binary for {arch}")
    platform = manifest["platforms"][arch]
    directory = f"{workspace}/.kapy-tools/apply-patch/{platform['bundle_sha256']}"
    binary = f"{directory}/apply_patch"
    # Check the actual executable hash, not merely whether a path exists.
    installed = output_text(
        await host.run(
            (
                "/bin/sh",
                "-c",
                'if [ -x "$1" ]; then sha256sum -- "$1"; fi',
                "kapy",
                binary,
            )
        )
    )
    if installed.split(" ", 1)[0].strip() != platform["files"]["apply_patch"]["sha256"]:
        await host.run(("mkdir", "-p", "--", directory))
        for name, metadata in platform["files"].items():
            data = resources.joinpath(arch, name).read_bytes()
            if (
                len(data) != metadata["bytes"]
                or hashlib.sha256(data).hexdigest() != metadata["sha256"]
            ):
                raise ValueError("Bundled apply_patch resource failed verification")
            await host.push(f"{directory}/{name}", data)
        await host.run(("chmod", "700", "--", binary))
    return replace(command, argv=(binary, *command.argv[1:]))


def apply_patch_plugin() -> ScriptTool[Patch]:
    resource = files("kapy.agent").joinpath("resources/apply_patch/description.md")
    return ScriptTool(
        "apply_patch",
        resource.read_text(),
        Patch,
        lambda args: ProcessCommand(("apply_patch",), stdin=args.patch.encode()),
        prepare,
    )

import base64
import hashlib
import io
import zipfile
from uuid import UUID, uuid4

import pytest

from kapy.gateway.auth import Principal
from kapy.rpc import RpcError

from .test_control import OPERATOR, create

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def archive(text="first"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as zip:
        zip.writestr(
            "example/SKILL.md", "---\nname: example\ndescription: Example skill\n---\n" + text
        )
        zip.writestr("example/data.txt", "data" * 40_000)
    return output.getvalue()


class Files:
    def __init__(self, data):
        self.data = data
        self.transfers = {}
        self.calls = []
        self.corrupt = False

    async def call(self, machine, method, params, *, timeout=60):  # noqa: ASYNC109
        self.calls.append((method, params))
        key = params["transfer_id"]
        if method in {"file.pull", "file.push"}:
            if key not in self.transfers:
                self.transfers[key] = {
                    "state": "open",
                    "size": len(self.data) if method == "file.pull" else params["size"],
                    "offset": 0,
                    "direction": method[5:],
                    "bytes": bytearray(),
                }
            return dict(self.transfers[key])
        entry = self.transfers[key]
        if method == "file.abort":
            entry["state"] = "aborted"
            return {"aborted": True}
        if method == "file.finish":
            if entry["offset"] == entry["size"]:
                entry["state"] = "complete"
            return dict(entry)
        offset = params["offset"]
        if entry["direction"] == "pull":
            block = self.data[offset : offset + params["max_bytes"]]
            entry["offset"] = offset + len(block)
            return {
                "data_base64": base64.b64encode(block).decode(),
                "start": offset,
                "next": entry["offset"] + int(self.corrupt),
                "eof": entry["offset"] == len(self.data),
            }
        block = base64.b64decode(params["data_base64"])
        entry["bytes"].extend(block)
        entry["offset"] += len(block)
        return {"next": entry["offset"]}


async def test_real_skill_storage_replay_cas_creator_and_same_version_download(
    gateway, monkeypatch
):
    sid = (await create(gateway))["session"]["id"]
    caller = Principal("session", "one", UUID(sid))
    files = Files(archive())
    monkeypatch.setattr(gateway.machines, "call", files.call)
    params = {
        "session_id": sid,
        "machine_id": "one",
        "archive_path": "/work/skill.zip",
        "request_id": str(uuid4()),
    }
    info = await gateway.call("skill.create", params, principal=caller)
    count = len(files.calls)
    assert await gateway.call("skill.create", params, principal=caller) == info
    assert len(files.calls) == count
    assert info["sha256"] == hashlib.sha256(files.data).hexdigest()
    assert len([method for method, _ in files.calls if method == "file.chunk"]) >= 3
    files.data = archive("updated")
    update = {
        **params,
        "skill_id": info["id"],
        "expected_revision": info["revision"],
        "request_id": str(uuid4()),
    }
    updated = await gateway.call("skill.update", update, principal=caller)
    assert updated["revision"] == 2
    with pytest.raises(RpcError) as error:
        await gateway.call("skill.update", {**update, "request_id": str(uuid4())}, principal=caller)
    assert error.value.code == -32009
    download = await gateway.call(
        "skill.download",
        {
            **params,
            "request_id": str(uuid4()),
            "skill_id": info["id"],
            "expected_revision": 2,
            "archive_path": "/work/download.zip",
        },
        principal=caller,
    )
    assert download["revision"] == 2
    pushed = [value for value in files.transfers.values() if value["direction"] == "push"]
    assert hashlib.sha256(pushed[0]["bytes"]).hexdigest() == download["sha256"]
    stranger_sid = (await create(gateway))["session"]["id"]
    stranger = Principal("session", "one", UUID(stranger_sid))
    with pytest.raises(RpcError) as error:
        await gateway.call(
            "skill.delete",
            {"skill_id": info["id"], "expected_revision": 2, "request_id": str(uuid4())},
            principal=stranger,
        )
    assert error.value.code == -32001


async def test_corrupt_transfer_aborts_without_publishing_half_archive(gateway, monkeypatch):
    sid = (await create(gateway))["session"]["id"]
    files = Files(archive())
    files.corrupt = True
    monkeypatch.setattr(gateway.machines, "call", files.call)
    with pytest.raises(RpcError) as error:
        await gateway.call(
            "skill.create",
            {
                "session_id": sid,
                "archive_path": "/source",
                "request_id": str(uuid4()),
                "machine_id": "one",
            },
            principal=OPERATOR,
        )
    assert error.value.code == -32021
    assert files.calls[-1][0] == "file.abort"
    assert await gateway.skills.catalog() == ()

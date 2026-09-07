"""Bounded archive exchange using Execution's existing transfer protocol."""

import asyncio
import base64
import binascii
import hashlib
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid5

from kapy.rpc import JsonObject, JsonValue, RpcError
from kapy.skills import InvalidSkill, SkillConflict, SkillNotFound, SkillTooLarge

from .auth import Principal
from .control import plain

if TYPE_CHECKING:
    from .control import ControlService

MAX_ARCHIVE = 16 * 1024 * 1024
CHUNK = 65_536


def fault(kind: str, message: str) -> RpcError:
    return RpcError(
        {"resource_limit": -32020, "conflict": -32009}.get(kind, -32021), message, {"kind": kind}
    )


def object_result(value: JsonValue) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise fault("io_error", "Invalid machine transfer response")
    return value


async def dispatch_skill(
    control: ControlService,
    method: str,
    data: dict[str, Any],
    principal: Principal,
    request: dict[str, Any] | None,
) -> JsonValue:
    try:
        if method == "skill.list":
            items = await control.skills.catalog(
                data["query"],
                after_id=data["after_id"],
                limit=data["limit"] + 1,
            )
            page = items[: data["limit"]]
            return {
                "items": plain(page),
                "next_after_id": (page[-1].id if len(items) > len(page) else None),
            }
        if method == "skill.get":
            return plain((await control.skills.get(data["skill_id"])).info)
        if method == "skill.read":
            return {
                "skill_id": data["skill_id"],
                "markdown": (await control.skills.get(data["skill_id"])).skill_md,
            }
        scope = hashlib.sha256(principal.id.encode()).hexdigest()
        request_id = data["request_id"]
        request_key = f"gateway:{scope}:{request_id}"
        if method == "skill.delete":
            await control.skills.delete(
                data["skill_id"],
                expected_revision=data["expected_revision"],
                request_key=request_key,
            )
            await control.metadata.rows(
                "UPDATE gateway_skill_access SET deleted=true WHERE skill_id=%s",
                (data["skill_id"],),
            )
            return {"skill_id": data["skill_id"], "deleted": True}
        assert request is not None
        async with control.skill_slots:
            session = await control.sessions.get_session(data["session_id"])
            machine_id = data["machine_id"] or session.default_machine_id
            if machine_id is None or machine_id not in session.machine_ids:
                raise RpcError(-32602, "An associated machine is required")
            exchange = Exchange(control, data, request, scope, machine_id)
            if method == "skill.download":
                return await exchange.download()
            archive = await exchange.upload()
            if method == "skill.create":
                info = await control.skills.create(archive, request_key=request_key)
                result = plain(info)
                await control.metadata.finish(
                    request_id,
                    result,
                    target=session.id,
                    skill_id=info.id,
                    creator=principal.id,
                )
                return result
            return plain(
                await control.skills.update(
                    data["skill_id"],
                    archive,
                    expected_revision=data["expected_revision"],
                    request_key=request_key,
                )
            )
    except (InvalidSkill, SkillNotFound, SkillConflict, SkillTooLarge) as exc:
        code, kind = {
            InvalidSkill: (-32602, "invalid_skill"),
            SkillNotFound: (-32004, "not_found"),
            SkillConflict: (-32009, "conflict"),
            SkillTooLarge: (-32020, "resource_limit"),
        }[type(exc)]
        raise RpcError(code, kind.replace("_", " "), {"kind": kind}) from None


class Exchange:
    def __init__(
        self,
        control: ControlService,
        data: dict[str, Any],
        request: dict[str, Any],
        scope: str,
        machine_id: str,
    ) -> None:
        self.control = control
        self.data = data
        self.op = dict(request["operation"])
        self.scope = scope
        self.machine = machine_id
        self.request_id: UUID = data["request_id"]
        self.base: JsonObject = {"session_id": str(data["session_id"])}

    async def save(self) -> None:
        await self.control.metadata.operation(self.request_id, self.op)

    async def call(self, method: str, **extra: Any) -> dict[str, Any]:
        return object_result(
            await self.control.machines.call(
                self.machine,
                method,
                {**self.base, **extra},
            )
        )

    async def identify(self, direction: str) -> None:
        attempt = self.op.setdefault("attempt", 0)
        self.base["transfer_id"] = str(
            uuid5(
                self.request_id,
                f"gateway:{self.scope}:skill:{direction}:{attempt}",
            )
        )
        self.op.update(machine_id=self.machine, transfer_id=self.base["transfer_id"])
        await self.save()

    async def abort(self) -> None:
        try:
            async with asyncio.timeout(5):
                await self.call("file.abort")
        except RpcError, TimeoutError:
            pass

    async def begin(self, direction: str, **extra: Any) -> dict[str, Any]:
        await self.identify(direction)
        info = await self.call(
            "file." + direction,
            path=self.data["archive_path"],
            transport={"kind": "websocket"},
            **extra,
        )
        if (
            info["state"] in {"failed", "aborted"}
            or direction == "pull"
            and info["state"] == "complete"
        ):
            self.op["attempt"] += 1
            await self.identify(direction)
            info = await self.call(
                "file." + direction,
                path=self.data["archive_path"],
                transport={"kind": "websocket"},
                **extra,
            )
        return info

    async def upload(self) -> bytes:
        try:
            info = await self.begin("pull")
            size = info["size"]
            if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_ARCHIVE:
                raise fault("resource_limit", "Skill archive exceeds 16 MiB")
            archive = bytearray()
            while len(archive) < size:
                chunk = await self.call("file.chunk", offset=len(archive), max_bytes=CHUNK)
                try:
                    decoded = base64.b64decode(chunk["data_base64"], validate=True)
                except KeyError, ValueError, TypeError, binascii.Error:
                    raise fault("io_error", "Invalid archive chunk") from None
                if not decoded or len(decoded) > CHUNK or len(archive) + len(decoded) > size:
                    raise fault("io_error", "Invalid archive chunk size")
                if chunk.get("next") != len(archive) + len(decoded):
                    raise fault("io_error", "Non-contiguous archive chunk")
                archive.extend(decoded)
            digest = hashlib.sha256(archive).hexdigest()
            if "sha256" in self.op and self.op["sha256"] != digest:
                raise fault("conflict", "Archive changed for this request")
            self.op.update(sha256=digest, archive_bytes=size)
            await self.save()
            finished = await self.call("file.finish", wait_ms=0)
            if finished.get("state") != "complete":
                raise fault("io_error", "Archive transfer did not complete")
            return bytes(archive)
        except BaseException:
            await asyncio.shield(self.abort())
            raise

    async def download(self) -> JsonValue:
        try:
            if "revision" in self.op:
                await self.identify("push")
                try:
                    status = await self.call("file.finish", wait_ms=0)
                except RpcError as exc:
                    if exc.code != -32004:
                        raise
                else:
                    if status.get("state") == "complete":
                        return self.result()
            info, archive = await self.control.skills.download(
                self.data["skill_id"],
                expected_revision=self.op.get(
                    "revision",
                    self.data["expected_revision"],
                ),
            )
            if (
                len(archive) > MAX_ARCHIVE
                or len(archive) != info.archive_bytes
                or (hashlib.sha256(archive).hexdigest() != info.sha256)
            ):
                raise fault("io_error", "Stored archive metadata does not match content")
            self.op.update(revision=info.revision, sha256=info.sha256, archive_bytes=len(archive))
            await self.save()
            status = await self.begin("push", size=len(archive), sha256=info.sha256)
            if status["state"] != "complete":
                offset = status["offset"]
                if not isinstance(offset, int) or not 0 <= offset <= len(archive):
                    raise fault("io_error", "Invalid transfer offset")
                while offset < len(archive):
                    block = archive[offset : offset + CHUNK]
                    result = await self.call(
                        "file.chunk",
                        offset=offset,
                        data_base64=base64.b64encode(block).decode("ascii"),
                    )
                    offset += len(block)
                    if result.get("next") != offset:
                        raise fault("io_error", "Invalid transfer acknowledgement")
                status = await self.call("file.finish", wait_ms=0)
                if status.get("state") != "complete":
                    raise fault("io_error", "Archive transfer did not complete")
            return self.result()
        except BaseException:
            if "transfer_id" in self.base:
                await asyncio.shield(self.abort())
            raise

    def result(self) -> JsonObject:
        return {
            "skill_id": self.data["skill_id"],
            "archive_path": self.data["archive_path"],
            "revision": self.op["revision"],
            "sha256": self.op["sha256"],
            "archive_bytes": self.op["archive_bytes"],
        }

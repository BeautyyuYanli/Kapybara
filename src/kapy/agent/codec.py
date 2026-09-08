"""Pydantic message persistence with media hydration and bounded State envelopes."""

import copy
import json
from dataclasses import asdict
from typing import Any, cast
from uuid import UUID

from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ToolReturnPart,
)

from kapy.state import CheckpointWrite, JsonObject, MessageWrite, RunnerState
from kapy.state.encoding import bounded

from .payloads import AgentPayloadStore, PayloadRef
from .types import AgentResourceLimit

CODEC = "kapy.agent.v3"
MESSAGE_LIMIT = 256 * 1024
DELTA_LIMIT = 16 * 1024
CHECKPOINT_LIMIT = 4 * 1024 * 1024
INLINE_LIMIT = 2 * 1024 * 1024


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def encode_message(message: ModelMessage) -> dict[str, Any]:
    """Encode a message whose media has already been externalized, if any."""
    data = ModelMessagesTypeAdapter.dump_python([message], mode="json")[0]
    if len(json_bytes(data)) > MESSAGE_LIMIT:
        raise AgentResourceLimit("Complete model message exceeds 256 KiB")
    return data


def message_write(encoded: dict[str, Any]) -> MessageWrite:
    """Build and check the actual State envelope, including metadata and tool-call IDs."""
    message_id = UUID(encoded["metadata"]["kapy_message_id"])
    text = "\n".join(
        part["content"]
        for part in encoded["parts"]
        if part["part_kind"] in {"text", "user-prompt"} and isinstance(part["content"], str)
    )
    write = MessageWrite(
        message_id,
        "model_request" if encoded["kind"] == "request" else "model_response",
        text,
        cast(JsonObject, encoded),
    )
    if len(json_bytes({**asdict(write), "message_id": str(message_id)})) > MESSAGE_LIMIT:
        raise AgentResourceLimit("Complete message envelope exceeds 256 KiB")
    bounded(write)  # State checks this same envelope using its conservative wire encoding.
    return write


class MessageCodec:
    def __init__(self, store: AgentPayloadStore, session_id: UUID) -> None:
        self.store = store
        self.session_id = session_id

    async def encode(self, message: ModelMessage) -> dict[str, Any]:
        message = copy.deepcopy(message)
        if isinstance(message, ModelRequest):
            message.instructions = None  # Stored once in the immutable instruction snapshot.
            for part in message.parts:
                if not isinstance(part, ToolReturnPart) or not isinstance(part.content, list):
                    continue
                refs = []
                for position, value in enumerate(part.content):
                    if isinstance(value, BinaryContent):
                        ref = await self.store.put(self.session_id, value.data)
                        refs.append(
                            {
                                "position": position,
                                "media_type": value.media_type,
                                "ref": asdict(ref),
                            }
                        )
                        part.content[position] = f"[media payload {ref.sha256}; {ref.bytes} bytes]"
                if refs:
                    part.metadata = {**(part.metadata or {}), "kapy_media_refs": refs}
        return encode_message(message)

    async def decode(self, data: dict[str, Any]) -> ModelMessage:
        message = ModelMessagesTypeAdapter.validate_python([data])[0]
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if not isinstance(part, ToolReturnPart):
                    continue
                refs = (part.metadata or {}).get("kapy_media_refs", [])
                for item in refs:
                    if not isinstance(part.content, list):
                        raise ValueError("Media reference has no content array")
                    ref = PayloadRef(**item["ref"])
                    part.content[item["position"]] = BinaryContent(
                        await self.store.get(self.session_id, ref), media_type=item["media_type"]
                    )
        return message

    async def state(self, data: dict[str, Any]) -> RunnerState:
        raw = json_bytes(data)
        if len(raw) > INLINE_LIMIT:
            ref = await self.store.put(self.session_id, raw)
            return RunnerState(CODEC, {"version": 3, "payload": cast(JsonObject, asdict(ref))})
        return RunnerState(CODEC, cast(JsonObject, copy.deepcopy(data)))

    async def load(self, state: RunnerState) -> dict[str, Any]:
        if state.codec != CODEC:
            raise ValueError(f"Unsupported runner codec: {state.codec}")
        data = copy.deepcopy(state.data)
        if "payload" in data:
            ref = PayloadRef(**cast(dict[str, Any], data["payload"]))
            data = json.loads(await self.store.get(self.session_id, ref))
        if data.get("version") != 3:
            raise ValueError("Unsupported runner state version")
        return data


def check_checkpoint(write: CheckpointWrite) -> None:
    value = {
        "number": write.number,
        "state": asdict(write.state),
        "messages": [{**asdict(m), "message_id": str(m.message_id)} for m in write.messages],
        "consumed_input_ids": [str(i) for i in write.consumed_input_ids],
    }
    if len(json_bytes(value)) > CHECKPOINT_LIMIT:
        raise AgentResourceLimit("Checkpoint exceeds 4 MiB")

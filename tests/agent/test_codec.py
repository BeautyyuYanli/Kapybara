from typing import Any, cast
from uuid import uuid4

import pytest
from pydantic_ai.messages import BinaryContent, ModelRequest, ToolReturnPart

from kapy.agent import AgentPayloadStore, AgentResourceLimit
from kapy.agent.codec import MessageCodec

from .test_runner import Payloads  # type: ignore[missing-import]


@pytest.mark.asyncio
async def test_media_and_large_context_roundtrip_without_machine() -> None:
    store = Payloads()
    session = uuid4()
    codec = MessageCodec(cast(AgentPayloadStore, store), session)
    original = ModelRequest(
        [
            ToolReturnPart(
                "read_media",
                ["image", BinaryContent(b"image bytes", media_type="image/png")],
                "original-call",
            )
        ]
    )
    encoded = await codec.encode(original)
    assert encoded["parts"][0]["metadata"]["kapy_media_refs"]
    assert isinstance(cast(Any, original.parts[0]).content[1], BinaryContent)
    restored = await MessageCodec(cast(AgentPayloadStore, store), session).decode(encoded)
    part = cast(ToolReturnPart, restored.parts[0])
    assert cast(Any, part.content)[1].data == b"image bytes"
    state = {"version": 1, "instructions": "x" * (2 * 1024 * 1024)}
    reference = await codec.state(state)
    assert "payload" in reference.data
    assert await codec.load(reference) == state


@pytest.mark.asyncio
async def test_oversized_message_is_rejected() -> None:
    codec = MessageCodec(cast(AgentPayloadStore, Payloads()), uuid4())
    with pytest.raises(AgentResourceLimit):
        await codec.encode(ModelRequest.user_text_prompt("x" * (256 * 1024)))

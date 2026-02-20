from __future__ import annotations

import pytest
from pydantic import ValidationError

from k.agent.core.entities import Event
from k.agent.memory.entities import MemoryRecord


def test_event_normalizes_same_out_channel_to_none() -> None:
    event = Event(
        in_channel="telegram/chat/1",
        out_channel="telegram/chat/1",
        content="hello",
    )
    assert event.out_channel is None
    assert event.effective_out_channel == "telegram/chat/1"


def test_event_accepts_legacy_kind_payload() -> None:
    event = Event.model_validate_json('{"kind":"telegram","content":"hi"}')
    assert event.in_channel == "telegram"
    assert event.out_channel is None


def test_memory_record_requires_in_channel() -> None:
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(
            {
                "kind": "telegram",
                "input": "in",
                "compacted": [],
                "output": "out",
                "detailed": [],
            }
        )

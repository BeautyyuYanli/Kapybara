from __future__ import annotations

import pytest
from pydantic import ValidationError

from k.agent.core.entities import Event
from k.agent.memory.entities import MemoryRecord


def test_event_normalizes_same_out_channel_to_none() -> None:
    event = Event(
        in_channel="telegram/chat/1",
        contact="telegram/42",
        out_channel="telegram/chat/1",
        content="hello",
    )
    assert event.out_channel is None
    assert event.effective_out_channel == "telegram/chat/1"


def test_event_requires_in_channel() -> None:
    with pytest.raises(ValidationError):
        Event.model_validate_json('{"contact":"telegram/1","content":"hi"}')


def test_event_allows_missing_contact() -> None:
    event = Event.model_validate_json('{"in_channel":"telegram/chat/1","content":"hi"}')
    assert event.contact is None


@pytest.mark.parametrize(
    "bad_contact",
    ["", "telegram", "telegram/123/extra", "/123", "telegram/"],
)
def test_event_rejects_bad_contact_format(bad_contact: str) -> None:
    with pytest.raises(ValidationError):
        Event(
            in_channel="telegram/chat/1",
            contact=bad_contact,
            content="hello",
        )


def test_memory_record_requires_in_channel() -> None:
    with pytest.raises(ValidationError):
        MemoryRecord.model_validate(
            {
                "input": "in",
                "compacted": [],
                "output": "out",
                "detailed": [],
            }
        )

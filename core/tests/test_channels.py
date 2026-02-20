from __future__ import annotations

import pytest

from k.agent.channels import (
    channel_has_prefix,
    channel_root,
    effective_out_channel,
    iter_channel_prefixes,
    normalize_out_channel,
    validate_channel_path,
)


def test_validate_channel_path_accepts_non_empty_segments() -> None:
    assert (
        validate_channel_path(
            "telegram/chat/123/thread/9",
            field_name="in_channel",
        )
        == "telegram/chat/123/thread/9"
    )


@pytest.mark.parametrize("bad", ["", " ", "/telegram", "telegram/", "telegram//chat"])
def test_validate_channel_path_rejects_invalid_shapes(bad: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        validate_channel_path(bad, field_name="in_channel")


def test_out_channel_helpers() -> None:
    assert normalize_out_channel(in_channel="telegram/chat/1", out_channel=None) is None
    assert (
        normalize_out_channel(
            in_channel="telegram/chat/1",
            out_channel="telegram/chat/1",
        )
        is None
    )
    assert (
        effective_out_channel(
            in_channel="telegram/chat/1",
            out_channel=None,
        )
        == "telegram/chat/1"
    )


def test_channel_path_helpers() -> None:
    assert channel_root("telegram/chat/1/thread/2") == "telegram"
    assert channel_has_prefix(
        channel="telegram/chat/1/thread/2",
        prefix="telegram/chat/1",
    )
    assert not channel_has_prefix(
        channel="telegram/chat/2/thread/2",
        prefix="telegram/chat/1",
    )
    assert iter_channel_prefixes("telegram/chat/1") == [
        "telegram",
        "telegram/chat",
        "telegram/chat/1",
    ]

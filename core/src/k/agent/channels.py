"""Shared helpers for hierarchical channel paths.

Channels are URL-path-like strings (slash-separated, no empty segments) used by
events and memory records to represent routing scopes such as:
`telegram/chat/<chat_id>/thread/<thread_id>`.
"""

from __future__ import annotations


def validate_channel_path(value: str, *, field_name: str) -> str:
    """Validate and return a normalized channel path.

    Rules:
    - must be non-empty
    - must not start or end with `/`
    - must not contain empty segments
    """

    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")

    channel = value.strip()
    if not channel:
        raise ValueError(f"{field_name} must not be empty")
    if channel.startswith("/") or channel.endswith("/"):
        raise ValueError(f"{field_name} must not start/end with '/': {value!r}")

    parts = channel.split("/")
    if any(not part for part in parts):
        raise ValueError(f"{field_name} contains empty path segment(s): {value!r}")
    return channel


def normalize_out_channel(*, in_channel: str, out_channel: str | None) -> str | None:
    """Return canonical out_channel where same-as-input is stored as `None`."""

    if out_channel is None:
        return None
    return None if out_channel == in_channel else out_channel


def effective_out_channel(*, in_channel: str, out_channel: str | None) -> str:
    """Return the resolved output channel (`out_channel` or fallback to input)."""

    return out_channel or in_channel


def channel_root(channel: str) -> str:
    """Return the first path segment of a channel."""

    return channel.split("/", 1)[0]


def channel_has_prefix(*, channel: str, prefix: str) -> bool:
    """Return true when `channel` is equal to `prefix` or in its subtree."""

    return channel == prefix or channel.startswith(prefix + "/")


def iter_channel_prefixes(channel: str) -> list[str]:
    """Return root-to-leaf channel prefixes."""

    parts = channel.split("/")
    return ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]

"""Telegram update -> agent Event conversion with channel derivation."""

from __future__ import annotations

import datetime
import json
from typing import Any

from k.agent.core import Event

from .compact import _compact_telegram_update, extract_chat_id
from .tz import _DEFAULT_TZINFO

_MESSAGE_OBJECT_PATHS: tuple[tuple[str, ...], ...] = (
    ("message",),
    ("edited_message",),
    ("channel_post",),
    ("edited_channel_post",),
    ("callback_query", "message"),
    ("business_message",),
    ("edited_business_message",),
)


def telegram_update_to_event(
    update: dict[str, Any],
    *,
    compact: bool = True,
    tz: datetime.tzinfo = _DEFAULT_TZINFO,
) -> Event:
    """Convert a Telegram update dict into an agent `Event`.

    When `compact=True` (default), the update is compacted before JSON
    serialization to reduce tokens while keeping routing-critical ids stable
    for downstream matchers.

    Channel mapping:
    - `in_channel`: `telegram/chat/<chat_id>` (+ `/thread/<message_thread_id>`
      only when the message is explicitly marked as a topic message)
    - `out_channel`: omitted (`None`), which means "same as input channel"
    """

    body = _json_dumps(_compact_telegram_update(update, tz=tz) if compact else update)
    return Event(in_channel=_in_channel_for_update(update), content=body)


def telegram_updates_to_event(
    updates: list[dict[str, Any]],
    *,
    compact: bool = True,
    tz: datetime.tzinfo = _DEFAULT_TZINFO,
) -> Event:
    """Convert multiple Telegram updates into a single agent `Event`.

    The returned `Event.content` is a newline-delimited stream of JSON objects
    (one Telegram update per line). For multi-update batches we keep a stable
    chat-level `in_channel` prefix (`telegram/chat/<chat_id>`) when all updates
    share the same chat, so retrieval can include all threads in that chat.
    """

    bodies = [
        _json_dumps(_compact_telegram_update(update, tz=tz) if compact else update)
        for update in updates
    ]
    return Event(in_channel=_in_channel_for_updates(updates), content="\n".join(bodies))


def telegram_update_to_event_json(
    update: dict[str, Any],
    *,
    compact: bool = True,
    tz: datetime.tzinfo = _DEFAULT_TZINFO,
) -> str:
    """Convert a Telegram update dict into an agent `Event` JSON string."""

    return telegram_update_to_event(update, compact=compact, tz=tz).model_dump_json()


def _json_dumps(obj: Any) -> str:
    """Token-friendly JSON.

    Notes:
    - Keep `ensure_ascii=False` so non-ASCII text doesn't bloat into `\\uXXXX`.
    - Minify separators to reduce prompt tokens.
    - Preserve insertion order (do not sort keys) so nested `"chat": {"id": ...}`
      / `"from": {"id": ...}` can keep `id` as the first key for downstream
      regex matchers that assume that layout.
    """

    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _in_channel_for_updates(updates: list[dict[str, Any]]) -> str:
    if not updates:
        return "telegram"
    if len(updates) == 1:
        return _in_channel_for_update(updates[0])

    chat_ids = {
        chat_id
        for update in updates
        if (chat_id := extract_chat_id(update)) is not None
    }
    if len(chat_ids) != 1:
        return "telegram"

    chat_id = next(iter(chat_ids))
    return f"telegram/chat/{chat_id}"


def _in_channel_for_update(update: dict[str, Any]) -> str:
    chat_id = extract_chat_id(update)
    if chat_id is None:
        return "telegram"

    channel = f"telegram/chat/{chat_id}"
    thread_id = _extract_message_thread_id(update)
    if thread_id is None:
        return channel
    return f"{channel}/thread/{thread_id}"


def _extract_nested_dict(
    update: dict[str, Any], path: tuple[str, ...]
) -> dict[str, Any] | None:
    cur: Any = update
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else None


def _extract_message_thread_id(update: dict[str, Any]) -> int | None:
    """Extract Telegram forum topic id from a message-like update payload.

    `message_thread_id` is meaningful for forum topic routing only when the
    message is explicitly marked as a topic message (`is_topic_message=true`).
    Avoid treating bare `message_thread_id` presence as a generic thread signal.
    """

    for path in _MESSAGE_OBJECT_PATHS:
        message = _extract_nested_dict(update, path)
        if message is None:
            continue

        if message.get("is_topic_message") is not True:
            continue

        thread_id = message.get("message_thread_id")
        if isinstance(thread_id, int):
            return thread_id
    return None

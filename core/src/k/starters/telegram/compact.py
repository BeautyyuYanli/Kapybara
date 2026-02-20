"""Compaction and trigger helpers for Telegram updates.

The compaction functions are intentionally lossy: they drop large/low-signal
fields while preserving routing-critical ids (`chat.id`, `from.id`) and the
human-readable content (`text` / `caption` / `data`). For readability, unix
seconds `date` values are rendered as ISO-8601 strings, with the original value
preserved as `date_unix`.
"""

from __future__ import annotations

import datetime
import json
from collections import defaultdict
from typing import Any, Final

from .tz import _format_unix_seconds

_SUPERGROUP_ID_PREFIX: Final[str] = "100"
_UPDATE_DATE_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("message", "date"),
    ("edited_message", "date"),
    ("channel_post", "date"),
    ("edited_channel_post", "date"),
    ("callback_query", "message", "date"),
    ("my_chat_member", "date"),
    ("chat_member", "date"),
    ("chat_join_request", "date"),
    ("business_message", "date"),
    ("edited_business_message", "date"),
)
_KEYWORD_TEXT_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("message", "text"),
    ("edited_message", "text"),
    ("channel_post", "text"),
    ("edited_channel_post", "text"),
    ("message", "caption"),
    ("edited_message", "caption"),
    ("channel_post", "caption"),
    ("edited_channel_post", "caption"),
    ("callback_query", "data"),
    ("inline_query", "query"),
)
_CHAT_ID_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("message", "chat", "id"),
    ("edited_message", "chat", "id"),
    ("channel_post", "chat", "id"),
    ("edited_channel_post", "chat", "id"),
    ("callback_query", "message", "chat", "id"),
    ("my_chat_member", "chat", "id"),
    ("chat_member", "chat", "id"),
    ("chat_join_request", "chat", "id"),
    ("business_message", "chat", "id"),
    ("edited_business_message", "chat", "id"),
)
_CHAT_TYPE_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("message", "chat", "type"),
    ("edited_message", "chat", "type"),
    ("channel_post", "chat", "type"),
    ("edited_channel_post", "chat", "type"),
    ("callback_query", "message", "chat", "type"),
    ("my_chat_member", "chat", "type"),
    ("chat_member", "chat", "type"),
    ("chat_join_request", "chat", "type"),
    ("business_message", "chat", "type"),
    ("edited_business_message", "chat", "type"),
)
_REPLY_TO_FROM_ID_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("message", "reply_to_message", "from", "id"),
    ("edited_message", "reply_to_message", "from", "id"),
)
_REPLY_TO_FROM_USERNAME_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("message", "reply_to_message", "from", "username"),
    ("edited_message", "reply_to_message", "from", "username"),
)
_FORUM_TOPIC_CREATED_PATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("message", "forum_topic_created"),
    ("edited_message", "forum_topic_created"),
    ("channel_post", "forum_topic_created"),
    ("edited_channel_post", "forum_topic_created"),
    ("callback_query", "message", "forum_topic_created"),
    ("business_message", "forum_topic_created"),
    ("edited_business_message", "forum_topic_created"),
)


def _expand_chat_id_watchlist(chat_ids: set[int]) -> set[int]:
    """Expand a chat-id watchlist to be resilient to Telegram supergroup IDs.

    Telegram supergroup/channel chat ids are often presented with a `-100...`
    prefix (e.g. `-1001886218691`). It's easy to copy/paste the shorter
    `-1886218691` form from other places. To reduce footguns, expand the
    watchlist to include both forms when the number appears to be a supergroup
    variant.
    """

    expanded: set[int] = set(chat_ids)
    for chat_id in list(chat_ids):
        if chat_id >= 0:
            continue

        abs_str = str(abs(chat_id))
        if abs_str.startswith(_SUPERGROUP_ID_PREFIX) and len(abs_str) > 3:
            expanded.add(-int(abs_str[3:]))
            continue

        expanded.add(-int(_SUPERGROUP_ID_PREFIX + abs_str))

    return expanded


def _compact_telegram_update(
    update: dict[str, Any], *, tz: datetime.tzinfo
) -> dict[str, Any]:
    """Return a compacted Telegram update payload.

    This is intentionally lossy: it drops large/low-signal fields (e.g. full
    `entities` arrays, media size/dimension metadata) but preserves:
    - update routing ids (`chat.id`, `from.id`) in the familiar nested form
    - human-readable message content (`text` / `caption` / `data`)
    - readable `date` strings (plus `date_unix` for the original seconds)
    - media `file_id` (for tracking/reuse), while omitting size/dimension noise
    - minimal context for replies and membership updates

    Important invariant for downstream tooling:
    - For any included `chat` or `from` object, `id` is emitted as the first key
      (e.g. `"from": {"id": 42, ...}`), matching the regex style used by
      `skills:context/telegram/stage_a` (relative to `~/.kapybara/skills`).
    """

    out: dict[str, Any] = {}
    update_id = update.get("update_id")
    if isinstance(update_id, int):
        out["update_id"] = update_id

    for key, val in update.items():
        if key == "update_id":
            continue
        if key in {
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "business_message",
            "edited_business_message",
        }:
            compacted = _compact_message(val, tz=tz)
        elif key == "callback_query":
            compacted = _compact_callback_query(val, tz=tz)
        elif key in {"my_chat_member", "chat_member"}:
            compacted = _compact_chat_member_update(val, tz=tz)
        elif key == "chat_join_request":
            compacted = _compact_chat_join_request(val, tz=tz)
        else:
            compacted = _compact_generic(val, tz=tz)

        if compacted is not None:
            out[key] = compacted

    return out


def _compact_user(user: Any) -> dict[str, Any] | None:
    if not isinstance(user, dict):
        return None
    user_id = user.get("id")
    if not isinstance(user_id, int):
        return None

    out: dict[str, Any] = {"id": user_id}
    username = user.get("username")
    if isinstance(username, str) and username:
        out["username"] = username

    first = user.get("first_name")
    last = user.get("last_name")
    if isinstance(first, str) or isinstance(last, str):
        first_s = first if isinstance(first, str) else ""
        last_s = last if isinstance(last, str) else ""
        name = (first_s + (" " if first_s and last_s else "") + last_s).strip()
        if name:
            out["name"] = name

    return out


def _compact_chat(chat: Any) -> dict[str, Any] | None:
    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    if not isinstance(chat_id, int):
        return None

    out: dict[str, Any] = {"id": chat_id}
    chat_type = chat.get("type")
    if isinstance(chat_type, str) and chat_type:
        out["type"] = chat_type

    title = chat.get("title")
    if isinstance(title, str) and title:
        out["title"] = title

    username = chat.get("username")
    if isinstance(username, str) and username:
        out["username"] = username

    first = chat.get("first_name")
    last = chat.get("last_name")
    if (not title and not username) and (
        isinstance(first, str) or isinstance(last, str)
    ):
        first_s = first if isinstance(first, str) else ""
        last_s = last if isinstance(last, str) else ""
        name = (first_s + (" " if first_s and last_s else "") + last_s).strip()
        if name:
            out["name"] = name

    return out


def _compact_photo_sizes(photo: Any) -> dict[str, Any] | None:
    if not isinstance(photo, list) or not photo:
        return None
    # Telegram sends multiple sizes; keep the biggest (typically last).
    last = photo[-1]
    if not isinstance(last, dict):
        return None
    out: dict[str, Any] = {}
    file_id = last.get("file_id")
    if isinstance(file_id, str) and file_id:
        out["file_id"] = file_id
    file_unique_id = last.get("file_unique_id")
    if isinstance(file_unique_id, str) and file_unique_id:
        out["file_unique_id"] = file_unique_id
    return out or None


def _compact_document_like(doc: Any) -> dict[str, Any] | None:
    if not isinstance(doc, dict):
        return None
    out: dict[str, Any] = {}
    file_id = doc.get("file_id")
    if isinstance(file_id, str) and file_id:
        out["file_id"] = file_id
    file_unique_id = doc.get("file_unique_id")
    if isinstance(file_unique_id, str) and file_unique_id:
        out["file_unique_id"] = file_unique_id
    return out or None


def _message_is_forum_topic_created(message: Any) -> bool:
    """Whether a message-like payload is a forum-topic-created service event."""

    return isinstance(message, dict) and "forum_topic_created" in message


def _compact_message(msg: Any, *, tz: datetime.tzinfo) -> dict[str, Any] | None:
    if not isinstance(msg, dict):
        return None

    out: dict[str, Any] = {}
    message_id = msg.get("message_id")
    if isinstance(message_id, int):
        out["message_id"] = message_id

    date = msg.get("date")
    if isinstance(date, int):
        out["date"] = _format_unix_seconds(date, tz=tz)
        out["date_unix"] = date

    chat = _compact_chat(msg.get("chat"))
    if chat is not None:
        out["chat"] = chat

    sender = _compact_user(msg.get("from"))
    if sender is not None:
        out["from"] = sender

    text = msg.get("text")
    if isinstance(text, str) and text:
        out["text"] = text

    caption = msg.get("caption")
    if isinstance(caption, str) and caption:
        out["caption"] = caption

    # Entities are verbose and primarily describe formatting; drop them to reduce tokens.

    reply_to = msg.get("reply_to_message")
    if isinstance(reply_to, dict) and not _message_is_forum_topic_created(reply_to):
        compact_reply = _compact_message(reply_to, tz=tz)
        if compact_reply:
            out["reply_to_message"] = compact_reply

    photo = _compact_photo_sizes(msg.get("photo"))
    if photo is not None:
        out["photo"] = photo

    for k in (
        "document",
        "video",
        "audio",
        "voice",
        "animation",
        "sticker",
        "video_note",
    ):
        compacted = _compact_document_like(msg.get(k))
        if compacted is not None:
            out[k] = compacted

    location = msg.get("location")
    if isinstance(location, dict):
        loc_out: dict[str, Any] = {}
        lat = location.get("latitude")
        lon = location.get("longitude")
        if isinstance(lat, (int, float)):
            loc_out["latitude"] = lat
        if isinstance(lon, (int, float)):
            loc_out["longitude"] = lon
        if loc_out:
            out["location"] = loc_out

    return out or None


def _compact_callback_query(val: Any, *, tz: datetime.tzinfo) -> dict[str, Any] | None:
    if not isinstance(val, dict):
        return None

    out: dict[str, Any] = {}
    cid = val.get("id")
    if isinstance(cid, str) and cid:
        out["id"] = cid

    sender = _compact_user(val.get("from"))
    if sender is not None:
        out["from"] = sender

    message = _compact_message(val.get("message"), tz=tz)
    if message is not None:
        out["message"] = message

    data = val.get("data")
    if isinstance(data, str) and data:
        out["data"] = data

    return out or None


def _compact_chat_member_update(
    val: Any, *, tz: datetime.tzinfo
) -> dict[str, Any] | None:
    if not isinstance(val, dict):
        return None

    out: dict[str, Any] = {}
    chat = _compact_chat(val.get("chat"))
    if chat is not None:
        out["chat"] = chat

    sender = _compact_user(val.get("from"))
    if sender is not None:
        out["from"] = sender

    date = val.get("date")
    if isinstance(date, int):
        out["date"] = _format_unix_seconds(date, tz=tz)
        out["date_unix"] = date

    new_member = val.get("new_chat_member")
    if isinstance(new_member, dict):
        nco: dict[str, Any] = {}
        user = _compact_user(new_member.get("user"))
        if user is not None:
            nco["user"] = user
        status = new_member.get("status")
        if isinstance(status, str) and status:
            nco["status"] = status
        if nco:
            out["new_chat_member"] = nco

    return out or None


def _compact_chat_join_request(
    val: Any, *, tz: datetime.tzinfo
) -> dict[str, Any] | None:
    if not isinstance(val, dict):
        return None
    out: dict[str, Any] = {}
    chat = _compact_chat(val.get("chat"))
    if chat is not None:
        out["chat"] = chat
    sender = _compact_user(val.get("from"))
    if sender is not None:
        out["from"] = sender
    date = val.get("date")
    if isinstance(date, int):
        out["date"] = _format_unix_seconds(date, tz=tz)
        out["date_unix"] = date
    bio = val.get("bio")
    if isinstance(bio, str) and bio:
        out["bio"] = bio
    return out or None


def _compact_generic(val: Any, *, tz: datetime.tzinfo) -> Any | None:
    # For unknown update types, keep only a small set of universally useful keys.
    if isinstance(val, dict):
        out: dict[str, Any] = {}

        # Keep id/date first for readability.
        for k in ("id", "date"):
            v = val.get(k)
            if k == "date" and isinstance(v, int):
                out["date"] = _format_unix_seconds(v, tz=tz)
                out["date_unix"] = v
            elif isinstance(v, (int, str)):
                out[k] = v

        chat = _compact_chat(val.get("chat"))
        if chat is not None:
            out["chat"] = chat

        sender = _compact_user(val.get("from"))
        if sender is not None:
            out["from"] = sender

        text = val.get("text")
        if isinstance(text, str) and text:
            out["text"] = text
        caption = val.get("caption")
        if isinstance(caption, str) and caption:
            out["caption"] = caption
        data = val.get("data")
        if isinstance(data, str) and data:
            out["data"] = data
        query = val.get("query")
        if isinstance(query, str) and query:
            out["query"] = query

        return out or None

    # Scalar values are already compact.
    if isinstance(val, (str, int, float, bool)) or val is None:
        return val

    return None


def extract_update_id(update: dict[str, Any]) -> int | None:
    """Extract `update_id` from a Telegram update dict (or return `None`)."""

    update_id = update.get("update_id")
    if isinstance(update_id, int):
        return update_id
    return None


def filter_unseen_updates(
    updates: list[dict[str, Any]],
    *,
    last_processed_update_id: int | None,
) -> list[dict[str, Any]]:
    """Filter out updates that are already processed or duplicates in the batch."""

    res: list[dict[str, Any]] = []
    seen: set[int] = set()

    for update in updates:
        update_id = extract_update_id(update)
        if update_id is None:
            continue
        if (
            last_processed_update_id is not None
            and update_id <= last_processed_update_id
        ):
            continue
        if update_id in seen:
            continue
        seen.add(update_id)
        res.append(update)

    return res


def _extract_nested_int(update: dict[str, Any], path: tuple[str, ...]) -> int | None:
    cur: Any = update
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, int) else None


def _extract_first_int(
    update: dict[str, Any], paths: tuple[tuple[str, ...], ...]
) -> int | None:
    for path in paths:
        val = _extract_nested_int(update, path)
        if val is not None:
            return val
    return None


def extract_update_date_unix_seconds(update: dict[str, Any]) -> int | None:
    """Best-effort extraction of an update's `date` (unix seconds)."""

    return _extract_first_int(update, _UPDATE_DATE_PATHS)


def _extract_nested_str(update: dict[str, Any], path: tuple[str, ...]) -> str | None:
    cur: Any = update
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, str) else None


def _extract_first_str(
    update: dict[str, Any], paths: tuple[tuple[str, ...], ...]
) -> str | None:
    for path in paths:
        val = _extract_nested_str(update, path)
        if val is not None:
            return val
    return None


def _extract_nested_dict(
    update: dict[str, Any], path: tuple[str, ...]
) -> dict[str, Any] | None:
    cur: Any = update
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur if isinstance(cur, dict) else None


def update_is_forum_topic_created(update: dict[str, Any]) -> bool:
    """Return whether an update is a forum-topic-created service message.

    Telegram carries this as `forum_topic_created` inside message-like payloads.
    Callers can use this to filter out topic-creation service noise before
    triggering/dispatching.
    """

    for path in _FORUM_TOPIC_CREATED_PATHS:
        if _extract_nested_dict(update, path) is not None:
            return True
    return False


def filter_non_forum_topic_created_updates(
    updates: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    """Drop updates that contain `forum_topic_created` service payloads.

    Returns:
        A tuple `(kept_updates, dropped_count)`.
    """

    kept: list[dict[str, Any]] = []
    dropped = 0
    for update in updates:
        if update_is_forum_topic_created(update):
            dropped += 1
            continue
        kept.append(update)
    return kept, dropped


def filter_updates_in_time_window(
    updates: list[dict[str, Any]],
    *,
    now_unix_seconds: int,
    window_seconds: int,
) -> list[dict[str, Any]]:
    """Return the subset of updates within the time window.

    Updates with no detectable `date` are kept.
    """

    if window_seconds < 0:
        raise ValueError(f"window_seconds must be >= 0; got {window_seconds}")

    res: list[dict[str, Any]] = []
    for update in updates:
        date = extract_update_date_unix_seconds(update)
        if date is None:
            res.append(update)
            continue
        age = now_unix_seconds - date
        if age <= window_seconds:
            res.append(update)
    return res


def extract_chat_id(update: dict[str, Any]) -> int | None:
    """Best-effort extraction of a Telegram update chat id."""

    return _extract_first_int(update, _CHAT_ID_PATHS)


def extract_chat_type(update: dict[str, Any]) -> str | None:
    """Best-effort extraction of a Telegram update chat type."""

    return _extract_first_str(update, _CHAT_TYPE_PATHS)


def update_is_private_chat(update: dict[str, Any]) -> bool:
    return extract_chat_type(update) == "private"


def update_mentions_bot(update: dict[str, Any], *, bot_username: str | None) -> bool:
    if not bot_username:
        return False
    text = _extract_first_str(update, _KEYWORD_TEXT_PATHS)
    if text is None:
        return False
    return f"@{bot_username}".casefold() in text.casefold()


def update_is_reply_to_bot(
    update: dict[str, Any],
    *,
    bot_user_id: int | None,
    bot_username: str | None,
) -> bool:
    reply_to_from_id = _extract_first_int(update, _REPLY_TO_FROM_ID_PATHS)
    if bot_user_id is not None and reply_to_from_id == bot_user_id:
        return True

    if bot_username:
        reply_to_username = _extract_first_str(update, _REPLY_TO_FROM_USERNAME_PATHS)
        if (
            reply_to_username
            and reply_to_username.casefold() == bot_username.casefold()
        ):
            return True

    return False


def chat_group_is_triggered(
    updates: list[dict[str, Any]],
    *,
    keyword: str,
    bot_user_id: int | None,
    bot_username: str | None,
) -> bool:
    for update in updates:
        if update_matches_keyword(update, keyword=keyword):
            return True
        if update_is_private_chat(update):
            return True
        if update_mentions_bot(update, bot_username=bot_username):
            return True
        if update_is_reply_to_bot(
            update, bot_user_id=bot_user_id, bot_username=bot_username
        ):
            return True

    return False


def trigger_flags_for_updates(
    updates: list[dict[str, Any]],
    *,
    keyword: str,
    bot_user_id: int | None,
    bot_username: str | None,
) -> dict[str, bool]:
    """Return which trigger conditions are present in an update list."""

    return {
        "keyword": any(update_matches_keyword(u, keyword=keyword) for u in updates),
        "private": any(update_is_private_chat(u) for u in updates),
        "mention": any(
            update_mentions_bot(u, bot_username=bot_username) for u in updates
        ),
        "reply": any(
            update_is_reply_to_bot(
                u, bot_user_id=bot_user_id, bot_username=bot_username
            )
            for u in updates
        ),
    }


def dispatch_groups_for_batch(
    updates: list[dict[str, Any]],
    *,
    keyword: str,
    chat_ids: set[int] | None,
    bot_user_id: int | None,
    bot_username: str | None,
) -> dict[int | None, list[dict[str, Any]]] | None:
    """Return chat groups to dispatch, or None if no trigger occurred.

    When `chat_ids` is provided, it acts as a trigger watchlist: only updates
    from those chats are considered for trigger evaluation. If a trigger
    occurs, the returned groups include *all* provided updates (regardless of
    chat id).
    """

    trigger_updates = (
        updates
        if chat_ids is None
        else [
            u
            for u in updates
            if (cid := extract_chat_id(u)) is not None and cid in chat_ids
        ]
    )

    if not trigger_updates:
        return None

    if not chat_group_is_triggered(
        trigger_updates,
        keyword=keyword,
        bot_user_id=bot_user_id,
        bot_username=bot_username,
    ):
        return None

    grouped = group_updates_by_chat_id(updates, chat_ids=None)
    return grouped or None


def group_updates_by_chat_id(
    updates: list[dict[str, Any]],
    *,
    chat_ids: set[int] | None,
) -> dict[int | None, list[dict[str, Any]]]:
    """Group updates by chat id.

    If `chat_ids` is not None, only include updates with a chat id in that set.
    Updates without a detectable chat id are grouped under `None` only when
    `chat_ids` is None.
    """

    grouped: dict[int | None, list[dict[str, Any]]] = defaultdict(list)
    for update in updates:
        chat_id = extract_chat_id(update)
        if chat_ids is not None and (chat_id is None or chat_id not in chat_ids):
            continue
        grouped[chat_id].append(update)
    return dict(grouped)


def update_matches_keyword(
    update: dict[str, Any],
    *,
    keyword: str,
) -> bool:
    keyword = keyword.strip()
    if not keyword:
        return False

    text = _extract_first_str(update, _KEYWORD_TEXT_PATHS)
    if text is None:
        # Fall back to searching the JSON representation so we still trigger on
        # less common update types without a known text field.
        text = json.dumps(update, ensure_ascii=False)

    return keyword.casefold() in text.casefold()

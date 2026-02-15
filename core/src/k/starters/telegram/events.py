"""Telegram update -> agent Event conversion."""

from __future__ import annotations

import datetime
import json
from typing import Any

from k.agent.core import Event

from .compact import _compact_telegram_update
from .tz import _DEFAULT_TZINFO


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
    """

    body = _json_dumps(_compact_telegram_update(update, tz=tz) if compact else update)
    return Event(kind="telegram", content=body)


def telegram_updates_to_event(
    updates: list[dict[str, Any]],
    *,
    compact: bool = True,
    tz: datetime.tzinfo = _DEFAULT_TZINFO,
) -> Event:
    """Convert multiple Telegram updates into a single agent `Event`.

    The returned `Event.content` is a newline-delimited stream of JSON objects
    (one Telegram update per line).
    """

    bodies = [
        _json_dumps(_compact_telegram_update(update, tz=tz) if compact else update)
        for update in updates
    ]
    return Event(kind="telegram", content="\n".join(bodies))


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

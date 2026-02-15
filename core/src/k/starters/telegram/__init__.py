"""Telegram long-poll starter.

This package polls the Telegram Bot API `getUpdates` endpoint and forwards
keyword-triggered batches of updates to :func:`k.agent.core.agent_run` as an
`Event` with:

- `kind="telegram"`
- `content=<newline-delimited update JSON strings>`

Design notes / boundaries:
- This is a polling (no webhook) starter intended for local/dev usage.
- The forwarded `content` is a newline-delimited stream where each line is a
  compacted Telegram update JSON object. This keeps the payload structured so
  the agent can infer routing metadata (e.g. `chat.id`) later, while avoiding
  high-token, low-signal fields (e.g. file sizes, repeated user/chat profile
  fields).
  - Invariant: `chat.id` and `from.id` remain present in the familiar nested
    form (`"chat": {"id": ...}`, `"from": {"id": ...}`) so downstream regex
    matchers can continue to route by chat/user id.
- No outbound Telegram send is performed here; this package only consumes
  updates and creates agent memories.
- The starter tracks the latest consumed `update_id` in-memory only.
  - Restarts may reprocess updates that are still pending server-side.
- Updates are accumulated in-memory until a trigger condition is met. Once
  triggered, the starter forwards *all pending* updates, grouped by `chat.id`,
  and starts one background :func:`k.agent.core.agent_run` per chat group.
  - When `--chat_id` is provided, it is treated as a *trigger watchlist*:
    only updates from those chats can cause a trigger, but when a trigger
    occurs, *all pending* updates (from any chat) are dispatched.
- For readability, any compacted payload `date` field (unix seconds in Telegram)
  is rendered as an ISO-8601 datetime string in a configurable timezone.
  The original unix seconds are preserved as `date_unix`.

Trigger rules:
- If **any** pending update triggers, the starter dispatches **all** pending
  updates (grouped by `chat.id`) concurrently.
- Trigger conditions: keyword match, private chat, reply-to-bot, or @mention.

Implementation note:
- Internal logic is split across `k.starters.telegram.*` submodules, while this
  package re-exports the historical public surface for backwards compatibility.
"""

from __future__ import annotations

from .api import TelegramBotApi, TelegramBotApiError
from .cli import main
from .compact import (
    _expand_chat_id_watchlist,
    chat_group_is_triggered,
    dispatch_groups_for_batch,
    extract_chat_id,
    extract_chat_type,
    extract_update_date_unix_seconds,
    extract_update_id,
    filter_unseen_updates,
    filter_updates_in_time_window,
    group_updates_by_chat_id,
    trigger_flags_for_updates,
    update_is_private_chat,
    update_is_reply_to_bot,
    update_matches_keyword,
    update_mentions_bot,
)
from .events import (
    telegram_update_to_event,
    telegram_update_to_event_json,
    telegram_updates_to_event,
)
from .runner import _poll_and_run_forever
from .tz import (
    _DEFAULT_TIMEZONE,
    _DEFAULT_TZINFO,
    _format_unix_seconds,
    _parse_timezone,
)

__all__ = [
    "_DEFAULT_TIMEZONE",
    "_DEFAULT_TZINFO",
    "TelegramBotApi",
    "TelegramBotApiError",
    "_expand_chat_id_watchlist",
    "_format_unix_seconds",
    "_parse_timezone",
    "_poll_and_run_forever",
    "chat_group_is_triggered",
    "dispatch_groups_for_batch",
    "extract_chat_id",
    "extract_chat_type",
    "extract_update_date_unix_seconds",
    "extract_update_id",
    "filter_unseen_updates",
    "filter_updates_in_time_window",
    "group_updates_by_chat_id",
    "main",
    "telegram_update_to_event",
    "telegram_update_to_event_json",
    "telegram_updates_to_event",
    "trigger_flags_for_updates",
    "update_is_private_chat",
    "update_is_reply_to_bot",
    "update_matches_keyword",
    "update_mentions_bot",
]

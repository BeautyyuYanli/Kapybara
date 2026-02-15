"""Telegram long-poll starter.

This module polls the Telegram Bot API `getUpdates` endpoint and forwards
keyword-triggered batches of updates to :func:`k.agent.core.agent_run` as an
`Event` with:

- `kind="telegram"`
- `content=<newline-delimited update JSON strings>`

Design notes / boundaries:
- This is a polling (no webhook) starter intended for local/dev usage.
- The forwarded `content` is a newline-delimited stream where each line is a
  full Telegram update JSON object. This keeps the payload structured so the
  agent can infer routing metadata (e.g. `chat.id`) later.
- No outbound Telegram send is performed here; this file only consumes updates
  and creates agent memories.
- The starter tracks the latest consumed `update_id` in-memory only.
  - Restarts may reprocess updates that are still pending server-side.
- Updates are accumulated in-memory until a trigger condition is met. Once
  triggered, the starter forwards *all pending* updates, grouped by `chat.id`,
  and starts one background :func:`k.agent.core.agent_run` per chat group.
  - When `--chat_id` is provided, it is treated as a *trigger watchlist*:
    only updates from those chats can cause a trigger, but when a trigger
    occurs, *all pending* updates (from any chat) are dispatched.

Trigger rules:
- If **any** pending update triggers, the starter dispatches **all** pending
  updates (grouped by `chat.id`) concurrently.
- Trigger conditions: keyword match, private chat, reply-to-bot, or @mention.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Final

import anyio
import anyio.to_thread as to_thread
import logfire
from pydantic_ai.models.openrouter import OpenRouterModel
from rich import print

from k.agent.core import Event, agent_run
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config

_TELEGRAM_API_BASE: Final[str] = "https://api.telegram.org"
_ID_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[,\s]+")
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


class TelegramBotApiError(RuntimeError):
    """Raised when Telegram Bot API returns a non-ok response or invalid JSON."""

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



def telegram_update_to_event(update: dict[str, Any]) -> Event:
    """Convert a Telegram update dict into an agent `Event`."""

    # Keep the update as structured JSON text so the agent can later extract
    # ids/metadata (e.g. `chat.id`, `message.from.id`) for routing responses.
    body = json.dumps(update, ensure_ascii=False)
    return Event(kind="telegram", content=body)


def telegram_updates_to_event(updates: list[dict[str, Any]]) -> Event:
    """Convert multiple Telegram updates into a single agent `Event`.

    The returned `Event.content` is a newline-delimited stream of JSON objects
    (one Telegram update per line).
    """

    bodies = [json.dumps(update, ensure_ascii=False) for update in updates]
    return Event(kind="telegram", content="\n".join(bodies))


def telegram_update_to_event_json(update: dict[str, Any]) -> str:
    """Convert a Telegram update dict into an agent `Event` JSON string."""

    return telegram_update_to_event(update).model_dump_json()


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
            update_is_reply_to_bot(u, bot_user_id=bot_user_id, bot_username=bot_username)
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


@dataclass(slots=True)
class TelegramBotApi:
    """Minimal Telegram Bot API client for `getUpdates` polling."""

    token: str

    def _method_url(self, method: str) -> str:
        # Never log/print this URL; it embeds the bot token.
        return f"{_TELEGRAM_API_BASE}/bot{self.token}/{method}"

    def _get_me_sync(self) -> dict[str, Any]:
        request = urllib.request.Request(self._method_url("getMe"), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as resp:
                raw = resp.read()
        except (
            urllib.error.HTTPError
        ) as e:  # pragma: no cover (hard to simulate reliably)
            raise TelegramBotApiError(f"Telegram getMe failed: HTTP {e.code}") from e
        except urllib.error.URLError as e:  # pragma: no cover (network dependent)
            raise TelegramBotApiError("Telegram getMe failed: network error") from e

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise TelegramBotApiError("Telegram getMe failed: invalid JSON") from e

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            desc = payload.get("description") if isinstance(payload, dict) else None
            raise TelegramBotApiError(
                "Telegram getMe failed"
                + (f": {desc}" if isinstance(desc, str) and desc else "")
            )

        result = payload.get("result")
        if not isinstance(result, dict):
            raise TelegramBotApiError("Telegram getMe failed: missing result dict")

        return result

    async def get_me(self) -> dict[str, Any]:
        """Fetch bot metadata via `getMe` (async wrapper)."""

        return await to_thread.run_sync(self._get_me_sync)

    def _get_updates_sync(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": timeout_seconds,
            # Telegram `getUpdates` `limit` is capped (commonly 100). Use the
            # maximum to drain pending updates without needing a CLI knob.
            "limit": 10,
        }
        if offset is not None:
            params["offset"] = offset

        url = f"{self._method_url('getUpdates')}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, method="GET")

        # Client timeout should exceed server long-poll timeout.
        client_timeout = max(5, timeout_seconds + 15)
        try:
            with urllib.request.urlopen(request, timeout=client_timeout) as resp:
                raw = resp.read()
        except (
            urllib.error.HTTPError
        ) as e:  # pragma: no cover (hard to simulate reliably)
            raise TelegramBotApiError(
                f"Telegram getUpdates failed: HTTP {e.code}"
            ) from e
        except urllib.error.URLError as e:  # pragma: no cover (network dependent)
            raise TelegramBotApiError(
                "Telegram getUpdates failed: network error"
            ) from e

        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise TelegramBotApiError("Telegram getUpdates failed: invalid JSON") from e

        if not isinstance(payload, dict) or payload.get("ok") is not True:
            desc = payload.get("description") if isinstance(payload, dict) else None
            raise TelegramBotApiError(
                "Telegram getUpdates failed"
                + (f": {desc}" if isinstance(desc, str) and desc else "")
            )

        result = payload.get("result")
        if not isinstance(result, list):
            raise TelegramBotApiError("Telegram getUpdates failed: missing result list")

        updates: list[dict[str, Any]] = []
        for item in result:
            if isinstance(item, dict):
                updates.append(item)
        return updates

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout_seconds: int,
    ) -> list[dict[str, Any]]:
        """Long-poll `getUpdates` (async wrapper).

        Note: stdlib `urllib` is blocking; this runs the request in a worker
        thread so the polling loop and agent tasks remain async-friendly.
        """

        return await to_thread.run_sync(
            lambda: self._get_updates_sync(
                offset=offset, timeout_seconds=timeout_seconds
            )
        )


async def _run_agent_for_chat_batch(
    chat_id: int | None,
    batch_updates: list[dict[str, Any]],
    model: OpenRouterModel,
    config: Config,
    memory_store: FolderMemoryStore,
    append_lock: anyio.Lock,
) -> None:
    try:
        output, mem = await agent_run(
            model=model,
            config=config,
            memory_store=memory_store,
            instruct=telegram_updates_to_event(batch_updates),
        )
    except Exception as e:  # pragma: no cover (model/runtime dependent)
        prefix = f"[chat_id={chat_id}] " if chat_id is not None else "[chat_id=?] "
        print(f"[red]agent_run failed[/red]: {prefix}{type(e).__name__}: {e}")
        return

    # `FolderMemoryStore.append()` mutates on-disk files; serialize appends
    # across concurrent chat runs to avoid corrupting `order.jsonl`.
    async with append_lock:
        await to_thread.run_sync(lambda: memory_store.append(mem))
    if output.strip():
        prefix = f"[chat_id={chat_id}] " if chat_id is not None else "[chat_id=?] "
        print(prefix + output)


def _prune_pending_updates_by_time_window(
    pending_updates_by_id: dict[int, dict[str, Any]],
    *,
    now_unix_seconds: int,
    window_seconds: int,
) -> None:
    if window_seconds < 0:
        raise ValueError(f"window_seconds must be >= 0; got {window_seconds}")

    to_drop: list[int] = []
    for update_id, update in pending_updates_by_id.items():
        date = extract_update_date_unix_seconds(update)
        if date is None:
            continue
        if now_unix_seconds - date > window_seconds:
            to_drop.append(update_id)

    for update_id in to_drop:
        del pending_updates_by_id[update_id]


async def _poll_and_run_forever(
    *,
    config: Config,
    model_name: str,
    token: str,
    timeout_seconds: int,
    keyword: str,
    time_window_seconds: int,
    chat_ids: set[int] | None,
) -> None:
    if timeout_seconds <= 0:
        raise ValueError(f"timeout_seconds must be > 0; got {timeout_seconds}")
    if not keyword.strip():
        raise ValueError(
            "Refusing to start with an empty --keyword. "
            "Set --keyword to the trigger substring."
        )
    if time_window_seconds < 0:
        raise ValueError(f"time_window_seconds must be >= 0; got {time_window_seconds}")

    mem_store = FolderMemoryStore(root=config.fs_base / "memories")
    model = OpenRouterModel(model_name)
    api = TelegramBotApi(token=token)
    try:
        me = await api.get_me()
    except TelegramBotApiError as e:
        print(f"[yellow]Telegram getMe failed[/yellow]: {e}")
        me = {}

    bot_user_id = me.get("id") if isinstance(me.get("id"), int) else None
    bot_username = me.get("username") if isinstance(me.get("username"), str) else None

    last_consumed_update_id: int | None = None

    next_offset: int | None = (
        last_consumed_update_id + 1 if last_consumed_update_id is not None else None
    )
    backoff_seconds = 1.0

    pending_updates_by_id: dict[int, dict[str, Any]] = {}
    append_lock = anyio.Lock()

    print(
        "\n".join(
            [
                "Telegram starter running (polling getUpdates).",
                f"- model: {model_name}",
                f"- timeout_seconds: {timeout_seconds}",
                f"- last_consumed_update_id: {last_consumed_update_id}",
                f"- keyword: {keyword!r}",
                f"- time_window_seconds: {time_window_seconds}",
                f"- chat_ids: {sorted(chat_ids) if chat_ids is not None else None}",
                f"- bot_user_id: {bot_user_id}",
                f"- bot_username: {bot_username}",
            ]
        )
    )

    async with anyio.create_task_group() as tg:
        while True:
            try:
                updates = await api.get_updates(
                    offset=next_offset,
                    timeout_seconds=timeout_seconds,
                )
            except TelegramBotApiError as e:
                print(f"[red]Telegram poll error[/red]: {e}")
                await anyio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 30.0)
                continue

            backoff_seconds = 1.0
            if updates:
                unseen_updates = filter_unseen_updates(
                    updates,
                    last_processed_update_id=last_consumed_update_id,
                )

                seen_chat_ids = sorted(
                    {
                        cid
                        for update in updates
                        if (cid := extract_chat_id(update)) is not None
                    }
                )
                chat_ids_preview = seen_chat_ids[:5] + (
                    ["..."] if len(seen_chat_ids) > 5 else []
                )
                print(
                    "[cyan]telegram recv[/cyan] "
                    + f"updates={len(updates)} unseen={len(unseen_updates)} "
                    + f"next_offset={next_offset} chats={chat_ids_preview or None}"
                )

                latest_observed_update_id = last_consumed_update_id
                accepted = 0
                watched = 0
                for update in unseen_updates:
                    update_id = extract_update_id(update)
                    if update_id is None:
                        continue

                    pending_updates_by_id.setdefault(update_id, update)
                    accepted += 1
                    if chat_ids is not None:
                        update_chat_id = extract_chat_id(update)
                        if update_chat_id is not None and update_chat_id in chat_ids:
                            watched += 1
                    if (
                        latest_observed_update_id is None
                        or update_id > latest_observed_update_id
                    ):
                        latest_observed_update_id = update_id
                if latest_observed_update_id is not None:
                    last_consumed_update_id = latest_observed_update_id
                    next_offset = last_consumed_update_id + 1
                if accepted:
                    print(
                        "[cyan]telegram pending[/cyan] "
                        + f"accepted={accepted} watched={watched if chat_ids is not None else None} pending={len(pending_updates_by_id)}"
                    )

            if not pending_updates_by_id:
                continue

            pending_updates_in_order = [
                pending_updates_by_id[update_id]
                for update_id in sorted(pending_updates_by_id)
            ]

            grouped = dispatch_groups_for_batch(
                pending_updates_in_order,
                keyword=keyword,
                chat_ids=chat_ids,
                bot_user_id=bot_user_id,
                bot_username=bot_username,
            )
            if not grouped:
                continue

            flags = trigger_flags_for_updates(
                pending_updates_in_order,
                keyword=keyword,
                bot_user_id=bot_user_id,
                bot_username=bot_username,
            )
            reasons = ",".join([k for k, v in flags.items() if v]) or "unknown"
            print(
                "[green]telegram trigger[/green] "
                + f"pending={len(pending_updates_in_order)} groups={len(grouped)} reasons={reasons}"
            )
            for cid, updates_for_chat in grouped.items():
                ids = [
                    uid
                    for update in updates_for_chat
                    if (uid := extract_update_id(update)) is not None
                ]
                id_span = f"{min(ids)}..{max(ids)}" if ids else "?"
                prefix = f"[chat_id={cid}]" if cid is not None else "[chat_id=?]"
                print(
                    "[green]telegram dispatch[/green] "
                    + f"{prefix} updates={len(updates_for_chat)} update_id={id_span}"
                )

            # Clear pending only when dispatching a triggered batch.
            pending_updates_by_id.clear()

            for cid, updates_for_chat in grouped.items():
                tg.start_soon(
                    _run_agent_for_chat_batch,
                    cid,
                    list(updates_for_chat),
                    model,
                    config,
                    mem_store,
                    append_lock,
                )


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="telegram",
        description="Telegram long-poll starter (getUpdates -> agent_run).",
    )
    parser.add_argument(
        "--model-name",
        default="openai/gpt-5.2",
        help="PydanticAI model name passed to OpenRouterModel.",
    )
    parser.add_argument(
        "--token",
        required=True,
        help="Telegram bot token (never printed).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=60,
        help="Telegram getUpdates long-poll timeout seconds.",
    )
    parser.add_argument(
        "--keyword",
        required=True,
        help="Trigger substring. When at least one in-window update matches, send the whole in-window batch to the agent.",
    )
    parser.add_argument(
        "--chat_id",
        default="",
        help="Optional comma/space separated chat ids. When set, only those chats are processed.",
    )
    parser.add_argument(
        "--time-window-seconds",
        type=int,
        default=60,
        help="Only include updates within this age when date is available.",
    )
    return parser.parse_args(argv)


async def main() -> None:
    """CLI entrypoint."""

    logfire.configure()
    logfire.instrument_pydantic_ai()

    args = _parse_cli_args()
    config = Config()  # type: ignore[call-arg]

    chat_ids: set[int] | None
    raw_chat_ids = str(args.chat_id).strip()
    if not raw_chat_ids:
        chat_ids = None
    else:
        parts = [p for p in _ID_SPLIT_RE.split(raw_chat_ids) if p]
        try:
            chat_ids = {int(p) for p in parts}
        except ValueError as e:
            raise ValueError(f"Invalid --chat_id entry in: {raw_chat_ids!r}") from e
        chat_ids = _expand_chat_id_watchlist(chat_ids)
    await _poll_and_run_forever(
        config=config,
        model_name=args.model_name,
        token=args.token,
        timeout_seconds=args.timeout_seconds,
        keyword=args.keyword,
        time_window_seconds=args.time_window_seconds,
        chat_ids=chat_ids,
    )


if __name__ == "__main__":
    anyio.run(main)

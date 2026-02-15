"""Polling loop and dispatch for the Telegram starter."""

from __future__ import annotations

import datetime
from typing import Any

import anyio
import anyio.to_thread as to_thread
from pydantic_ai.models.openrouter import OpenRouterModel
from rich import print

from k.agent.core import agent_run
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config

from .api import TelegramBotApi, TelegramBotApiError
from .compact import (
    dispatch_groups_for_batch,
    extract_chat_id,
    extract_update_date_unix_seconds,
    extract_update_id,
    filter_unseen_updates,
    trigger_flags_for_updates,
)
from .events import telegram_updates_to_event


async def _run_agent_for_chat_batch(
    chat_id: int | None,
    batch_updates: list[dict[str, Any]],
    model: OpenRouterModel,
    config: Config,
    memory_store: FolderMemoryStore,
    append_lock: anyio.Lock,
    tz: datetime.tzinfo,
) -> None:
    try:
        output, mem = await agent_run(
            model=model,
            config=config,
            memory_store=memory_store,
            instruct=telegram_updates_to_event(batch_updates, tz=tz),
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
    tz: datetime.tzinfo,
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
                f"- timezone: {tz}",
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
                    tz,
                )

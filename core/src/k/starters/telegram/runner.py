"""Polling loop and dispatch for the Telegram starter."""

from __future__ import annotations

import datetime
import html
from functools import partial
from pathlib import Path
from typing import Any

import anyio
import anyio.to_thread as to_thread
from pydantic_ai.models import Model
from pydantic_ai.models.openrouter import OpenRouterModel
from rich import print

from k.agent.core import agent_run
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config

from .api import TelegramBotApi, TelegramBotApiError
from .compact import (
    dispatch_groups_for_batch,
    extract_chat_id,
    extract_update_id,
    filter_unseen_updates,
    trigger_flags_for_updates,
)
from .events import telegram_updates_to_event
from .history import (
    append_updates_jsonl,
    load_last_trigger_update_id_by_chat,
    load_recent_updates_grouped_by_chat_id,
    save_last_trigger_update_id_by_chat,
    trigger_cursor_state_path_for_updates_store,
)


async def run_agent_for_chat_batch(
    api: TelegramBotApi,
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
        reply_to_message_id: int | None = None
        reply_chat_id: int | None = chat_id
        for update in reversed(batch_updates):
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            message_id = message.get("message_id")
            if not isinstance(message_id, int):
                continue
            reply_to_message_id = message_id
            if reply_chat_id is None:
                chat = message.get("chat")
                if isinstance(chat, dict) and isinstance(chat.get("id"), int):
                    reply_chat_id = chat["id"]
            break

        if reply_chat_id is not None and reply_to_message_id is not None:
            exc_type = html.escape(type(e).__name__)
            exc_msg = html.escape(str(e)) if str(e) else "Unknown error"
            text = (
                "<b>Agent error</b>\n"
                + f"<code>{exc_type}</code>: {exc_msg}\n"
                + "Check the server logs for a traceback."
            )
            try:
                await api.send_message(
                    chat_id=reply_chat_id,
                    text=text,
                    reply_to_message_id=reply_to_message_id,
                )
            except Exception as send_err:  # pragma: no cover (network dependent)
                print(
                    "[yellow]Telegram sendMessage failed[/yellow]: "
                    + f"{type(send_err).__name__}: {send_err}"
                )
        return

    # `FolderMemoryStore.append()` mutates on-disk files; serialize appends
    # across concurrent chat runs to avoid corrupting `order.jsonl`.
    async with append_lock:
        await to_thread.run_sync(memory_store.append, mem)
    if output.strip():
        prefix = f"[chat_id={chat_id}] " if chat_id is not None else "[chat_id=?] "
        print(prefix + output)


def overlay_dispatch_groups_with_recent(
    dispatch_groups: dict[int | None, list[dict[str, Any]]],
    *,
    recent_groups: dict[int | None, list[dict[str, Any]]],
) -> tuple[dict[int | None, list[dict[str, Any]]], int]:
    """Overlay dispatch groups with stored recent updates by chat id.

    Only chat ids already present in `dispatch_groups` are considered. This
    preserves the historical dispatch boundary (no extra chat runs are created).
    """

    selected: dict[int | None, list[dict[str, Any]]] = {}
    replaced = 0
    for chat_id, pending_updates in dispatch_groups.items():
        recent_updates = recent_groups.get(chat_id)
        if recent_updates:
            selected[chat_id] = recent_updates
            replaced += 1
            continue
        selected[chat_id] = pending_updates
    return selected, replaced


def filter_dispatch_groups_after_last_trigger(
    dispatch_groups: dict[int | None, list[dict[str, Any]]],
    *,
    last_trigger_update_id_by_chat: dict[int, int],
) -> tuple[dict[int | None, list[dict[str, Any]]], int, int]:
    """Keep only updates strictly newer than each chat's trigger cursor.

    For chats that already have a persisted cursor, updates with missing
    `update_id` are dropped because their ordering relative to the cursor is
    unknowable.
    """

    filtered: dict[int | None, list[dict[str, Any]]] = {}
    dropped_updates = 0
    dropped_groups = 0

    for chat_id, updates in dispatch_groups.items():
        if chat_id is None:
            filtered[chat_id] = updates
            continue

        cursor = last_trigger_update_id_by_chat.get(chat_id)
        if cursor is None:
            filtered[chat_id] = updates
            continue

        kept = []
        for update in updates:
            update_id = extract_update_id(update)
            if update_id is None or update_id <= cursor:
                dropped_updates += 1
                continue
            kept.append(update)

        if kept:
            filtered[chat_id] = kept
        else:
            dropped_groups += 1

    return filtered, dropped_updates, dropped_groups


def update_last_trigger_update_id_by_chat(
    state: dict[int, int],
    *,
    dispatched_groups: dict[int | None, list[dict[str, Any]]],
) -> int:
    """Advance in-memory trigger cursors from dispatched chat batches."""

    updated_chats = 0
    for chat_id, updates in dispatched_groups.items():
        if chat_id is None:
            continue
        max_update_id: int | None = None
        for update in updates:
            update_id = extract_update_id(update)
            if update_id is None:
                continue
            if max_update_id is None or update_id > max_update_id:
                max_update_id = update_id
        if max_update_id is None:
            continue

        prev = state.get(chat_id)
        if prev is None or max_update_id > prev:
            state[chat_id] = max_update_id
            updated_chats += 1

    return updated_chats


async def _poll_and_run_forever(
    *,
    config: Config,
    model: Model | str,
    token: str,
    timeout_seconds: int,
    keyword: str,
    chat_ids: set[int] | None,
    updates_store_path: Path | None = None,
    dispatch_recent_per_chat: int = 0,
    tz: datetime.tzinfo,
) -> None:
    if timeout_seconds <= 0:
        raise ValueError(f"timeout_seconds must be > 0; got {timeout_seconds}")
    if not keyword.strip():
        raise ValueError(
            "Refusing to start with an empty --keyword. "
            "Set --keyword to the trigger substring."
        )
    if dispatch_recent_per_chat < 0:
        raise ValueError(
            f"dispatch_recent_per_chat must be >= 0; got {dispatch_recent_per_chat}"
        )
    if dispatch_recent_per_chat > 0 and updates_store_path is None:
        raise ValueError(
            "dispatch_recent_per_chat requires updates_store_path to be configured"
        )

    mem_store = FolderMemoryStore(root=config.fs_base / "memories")
    if isinstance(model, str):
        model = OpenRouterModel(model)
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
    last_trigger_update_id_by_chat: dict[int, int] = {}
    trigger_cursor_state_path: Path | None = None
    if updates_store_path is not None:
        trigger_cursor_state_path = trigger_cursor_state_path_for_updates_store(
            updates_store_path
        )
        try:
            last_trigger_update_id_by_chat = await to_thread.run_sync(
                load_last_trigger_update_id_by_chat,
                trigger_cursor_state_path,
            )
        except (OSError, ValueError) as e:
            print(
                "[yellow]telegram trigger cursor load error[/yellow] "
                + f"path={trigger_cursor_state_path}: {type(e).__name__}: {e}"
            )
            last_trigger_update_id_by_chat = {}

    print(
        "\n".join(
            [
                "Telegram starter running (polling getUpdates).",
                f"- model: {model}",
                f"- timeout_seconds: {timeout_seconds}",
                f"- last_consumed_update_id: {last_consumed_update_id}",
                f"- keyword: {keyword!r}",
                f"- chat_ids: {sorted(chat_ids) if chat_ids is not None else None}",
                f"- updates_store_path: {updates_store_path}",
                f"- trigger_cursor_state_path: {trigger_cursor_state_path}",
                f"- loaded_trigger_cursor_chats: {len(last_trigger_update_id_by_chat)}",
                f"- dispatch_recent_per_chat: {dispatch_recent_per_chat}",
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
                accepted_updates: list[dict[str, Any]] = []
                for update in unseen_updates:
                    update_id = extract_update_id(update)
                    if update_id is None:
                        continue

                    pending_updates_by_id.setdefault(update_id, update)
                    accepted_updates.append(update)
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
                persisted = 0
                if updates_store_path is not None and accepted_updates:
                    try:
                        persisted = await to_thread.run_sync(
                            append_updates_jsonl,
                            updates_store_path,
                            list(accepted_updates),
                        )
                    except OSError as e:
                        print(
                            "[yellow]telegram persist error[/yellow] "
                            + f"path={updates_store_path}: {type(e).__name__}: {e}"
                        )
                if accepted:
                    print(
                        "[cyan]telegram pending[/cyan] "
                        + f"accepted={accepted} persisted={persisted if updates_store_path is not None else None} "
                        + f"watched={watched if chat_ids is not None else None} pending={len(pending_updates_by_id)}"
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

            # If chat_ids is provided, treat it as an exclusive filter for dispatching
            # to avoid duplicate processing in multi-instance setups.
            if chat_ids is not None:
                dispatch_groups = {
                    cid: updates for cid, updates in grouped.items() if cid in chat_ids
                }
            else:
                dispatch_groups = grouped

            if not dispatch_groups:
                # Trigger condition matched but no updates from watched chats to dispatch.
                # Clear pending to avoid re-evaluating the same batch.
                pending_updates_by_id.clear()
                continue

            dispatch_source = "pending"
            replaced_groups = 0
            if updates_store_path is not None and dispatch_recent_per_chat > 0:
                try:
                    recent_groups = await to_thread.run_sync(
                        partial(
                            load_recent_updates_grouped_by_chat_id,
                            updates_store_path,
                            per_chat_limit=dispatch_recent_per_chat,
                        )
                    )
                except (OSError, ValueError) as e:
                    print(
                        "[yellow]telegram recent load error[/yellow] "
                        + f"path={updates_store_path}: {type(e).__name__}: {e}"
                    )
                else:
                    dispatch_groups, replaced_groups = (
                        overlay_dispatch_groups_with_recent(
                            grouped,
                            recent_groups=recent_groups,
                        )
                    )
                    if replaced_groups:
                        dispatch_source = "stored_recent"

            cursor_dropped_updates = 0
            cursor_dropped_groups = 0
            dispatch_groups, cursor_dropped_updates, cursor_dropped_groups = (
                filter_dispatch_groups_after_last_trigger(
                    dispatch_groups,
                    last_trigger_update_id_by_chat=last_trigger_update_id_by_chat,
                )
            )
            if cursor_dropped_updates:
                dispatch_source += "+cursor"

            flags = trigger_flags_for_updates(
                pending_updates_in_order,
                keyword=keyword,
                bot_user_id=bot_user_id,
                bot_username=bot_username,
            )
            reasons = ",".join([k for k, v in flags.items() if v]) or "unknown"
            print(
                "[green]telegram trigger[/green] "
                + f"pending={len(pending_updates_in_order)} groups={len(dispatch_groups)} "
                + f"source={dispatch_source} replaced_groups={replaced_groups} "
                + f"cursor_dropped_updates={cursor_dropped_updates} cursor_dropped_groups={cursor_dropped_groups} "
                + f"reasons={reasons}"
            )

            if not dispatch_groups:
                print(
                    "[green]telegram dispatch[/green] "
                    + "skipped: no updates newer than last trigger cursor"
                )
                # Trigger condition already matched, so clear pending to avoid
                # repeatedly re-evaluating the same pre-cursor updates.
                pending_updates_by_id.clear()
                continue

            for cid, updates_for_chat in dispatch_groups.items():
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

            updated_cursor_chats = update_last_trigger_update_id_by_chat(
                last_trigger_update_id_by_chat,
                dispatched_groups=dispatch_groups,
            )
            if updated_cursor_chats and trigger_cursor_state_path is not None:
                try:
                    await to_thread.run_sync(
                        save_last_trigger_update_id_by_chat,
                        trigger_cursor_state_path,
                        dict(last_trigger_update_id_by_chat),
                    )
                except (OSError, ValueError) as e:
                    print(
                        "[yellow]telegram trigger cursor save error[/yellow] "
                        + f"path={trigger_cursor_state_path}: {type(e).__name__}: {e}"
                    )

            for cid, updates_for_chat in dispatch_groups.items():
                tg.start_soon(
                    run_agent_for_chat_batch,
                    api,
                    cid,
                    list(updates_for_chat),
                    model,
                    config,
                    mem_store,
                    append_lock,
                    tz,
                )

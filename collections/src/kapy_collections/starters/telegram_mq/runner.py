"""AMQP-backed Telegram starter runner.

This variant consumes Telegram-like updates from RabbitMQ and reuses the core
Telegram dispatch pipeline. String/omitted model inputs and Logfire setup are
kept aligned with the installed `kapy` CLI so long-running starters inherit the
same config-backed defaults.
"""

import datetime
import html
import importlib
import json
import os
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import anyio.to_thread as to_thread
from k.agent.core import agent_run
from k.agent.core.cli_runtime import configure_cli_logfire, resolve_cli_model
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config

from ..telegram.api import TelegramBotApi, TelegramBotApiError
from ..telegram.compact import (
    _expand_chat_id_watchlist,
    dispatch_groups_for_batch,
    extract_update_id,
    filter_non_forum_topic_created_updates,
    filter_unseen_updates,
)
from ..telegram.history import (
    append_updates_jsonl,
    load_last_trigger_update_id_by_chat,
    load_recent_updates_grouped_by_chat_id,
    save_last_trigger_update_id_by_chat,
    trigger_cursor_state_path_for_updates_store,
)
from ..telegram.runner import (
    _telegram_updates_to_event_text_only_compaction,
    cap_dispatch_groups_per_chat,
    filter_dispatch_groups_after_last_trigger,
    filter_dispatch_groups_without_forum_topic_created_updates,
    overlay_dispatch_groups_with_recent,
    update_last_trigger_update_id_by_chat,
)
from ..telegram.tz import _DEFAULT_TIMEZONE, _parse_timezone

if TYPE_CHECKING:
    pass


async def run_agent_for_chat_batch(
    api: TelegramBotApi | None,
    chat_id: int | None,
    batch_updates: list[dict[str, Any]],
    model: Any,
    config: Config,
    memory_store: FolderMemoryStore,
    append_lock: anyio.Lock,
    tz: datetime.tzinfo,
) -> None:
    try:
        mem = await agent_run(
            model=model,
            config=config,
            memory_store=memory_store,
            # Keep compaction policy aligned with Telegram polling starter:
            # compact only plain text updates; preserve other payloads.
            instruct=_telegram_updates_to_event_text_only_compaction(
                batch_updates,
                tz=tz,
            ),
            # Empty list means: auto-generate memories in `agent_run`.
            parent_memories=[],
        )
    except Exception as e:
        prefix = f"[chat_id={chat_id}] " if chat_id is not None else "[chat_id=?] "
        print(f"[red]agent_run failed[/red]: {prefix}{type(e).__name__}: {e}")

        if api is None:
            return

        reply_to_message_id: int | None = None
        reply_chat_id: int | None = chat_id
        message_thread_id: int | None = None
        for update in reversed(batch_updates):
            message = update.get("message")
            if not isinstance(message, dict):
                continue
            message_id = message.get("message_id")
            if not isinstance(message_id, int):
                continue
            reply_to_message_id = message_id
            message_thread_id = message.get("message_thread_id")
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
                    message_thread_id=message_thread_id,
                )
            except Exception as send_err:
                print(
                    "[yellow]Telegram sendMessage failed[/yellow]: "
                    + f"{type(send_err).__name__}: {send_err}"
                )
        return

    async with append_lock:
        await to_thread.run_sync(memory_store.append, mem)
    if mem.output.strip():
        prefix = f"[chat_id={chat_id}] " if chat_id is not None else "[chat_id=?] "
        print(prefix + mem.output)


def _mq_to_update(mq_msg: dict[str, Any]) -> dict[str, Any]:
    """Map custom MQ format to standard Telegram Update format."""
    dt_str = mq_msg.get("date")
    try:
        # Handle ISO format with potential Z or timezone offset
        if not isinstance(dt_str, str):
            raise ValueError("date must be an ISO-8601 string")
        dt = datetime.datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        ts = int(dt.timestamp())
    except Exception:
        ts = int(datetime.datetime.now().timestamp())

    # Build a fake Telegram Update object
    msg = {
        "message_id": mq_msg.get("message_id"),
        "date": ts,
        "chat": {"id": mq_msg.get("chat_id"), "type": "supergroup"},
        "from": {
            "id": mq_msg.get("sender_id"),
            "username": mq_msg.get("sender_username"),
            "first_name": mq_msg.get("sender_fullname"),
            "is_bot": mq_msg.get("is_bot", False),
        },
        "text": mq_msg.get("text"),
        "caption": mq_msg.get("caption"),
        "message_thread_id": mq_msg.get("message_thread_id"),
    }

    if mq_msg.get("is_reply") and mq_msg.get("reply_to"):
        reply = mq_msg["reply_to"]
        msg["reply_to_message"] = {
            "message_id": reply.get("message_id"),
            "from": {
                "id": reply.get("sender_id"),
                "username": reply.get("sender_username"),
                "first_name": reply.get("sender_fullname"),
            },
            "text": reply.get("text"),
        }

    # Use message_id as a surrogate for update_id
    return {"update_id": mq_msg.get("message_id"), "message": msg}


def _extract_update_from_user_id(update: dict[str, Any]) -> int | None:
    for top_key in (
        "message",
        "edited_message",
        "channel_post",
        "edited_channel_post",
        "business_message",
        "edited_business_message",
        "callback_query",
    ):
        val = update.get(top_key)
        if not isinstance(val, dict):
            continue
        sender = val.get("from")
        if isinstance(sender, dict):
            sender_id = sender.get("id")
            if isinstance(sender_id, int):
                return sender_id
        if top_key == "callback_query":
            msg = val.get("message")
            if not isinstance(msg, dict):
                continue
            sender = msg.get("from")
            if isinstance(sender, dict):
                sender_id = sender.get("id")
                if isinstance(sender_id, int):
                    return sender_id
    return None


async def run_amqp_forever(
    *,
    config: Config,
    model: Any,
    token: str | None,
    amqp_url: str,
    queue_name: str,
    keyword: str,
    chat_ids: set[int] | None,
    updates_store_path: Path | None = None,
    dispatch_recent_per_chat: int = 0,
    tz: datetime.tzinfo,
) -> None:
    """Run AMQP consumption once and propagate unexpected failures."""

    aio_pika = importlib.import_module("aio_pika")

    if dispatch_recent_per_chat < 0:
        raise ValueError(
            f"dispatch_recent_per_chat must be >= 0; got {dispatch_recent_per_chat}"
        )

    mem_store = FolderMemoryStore(root=config.config_base / "memories")
    append_lock = anyio.Lock()

    bot_user_id = None
    bot_username = None
    api: TelegramBotApi | None = None
    if token:
        api = TelegramBotApi(token=token)
        try:
            me = await api.get_me()
            bot_user_id = me.get("id")
            bot_username = me.get("username")
        except TelegramBotApiError as e:
            print(f"[yellow]Telegram getMe failed[/yellow]: {e}")

    last_consumed_update_id: int | None = None
    pending_updates_by_id: dict[int, dict[str, Any]] = {}

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
                "Telegram AMQP listener running.",
                f"- model: {model}",
                f"- amqp_url: {amqp_url}",
                f"- queue_name: {queue_name}",
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

    connection = await aio_pika.connect_robust(amqp_url)
    async with connection:
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=10)

        # Ensure we have our own queue to avoid missing messages due to other consumers
        # and bind it to the chats we care about.
        queue = await channel.declare_queue(exclusive=True)
        if chat_ids:
            for cid in chat_ids:
                # Standard routing key format for the userbot-listener
                routing_key = f"chat:{cid}"
                await queue.bind("telegram.messages", routing_key=routing_key)
                print(f"Bound to chat: {cid}")
        else:
            # Fallback to the provided queue name if no chat_ids specified
            queue = await channel.get_queue(queue_name)
        async with anyio.create_task_group() as tg, queue.iterator() as queue_iter:
            async for message in queue_iter:
                async with message.process():
                    try:
                        body = json.loads(message.body.decode())
                        # Convert to standard format
                        update = _mq_to_update(body) if "chat_id" in body else body
                    except Exception as e:
                        print(f"Failed to parse message: {e}")
                        continue

                    unseen_updates = filter_unseen_updates(
                        [update],
                        last_processed_update_id=last_consumed_update_id,
                    )
                    unseen_updates, _ignored_forum_topic_created_updates = (
                        filter_non_forum_topic_created_updates(unseen_updates)
                    )
                    if not unseen_updates:
                        continue

                    accepted_updates: list[dict[str, Any]] = []
                    latest_observed_update_id = last_consumed_update_id
                    for unseen in unseen_updates:
                        if bot_user_id is not None:
                            from_user_id = _extract_update_from_user_id(unseen)
                            if from_user_id == bot_user_id:
                                continue
                        update_id = extract_update_id(unseen)
                        if update_id is None:
                            continue
                        pending_updates_by_id.setdefault(update_id, unseen)
                        accepted_updates.append(unseen)
                        if (
                            latest_observed_update_id is None
                            or update_id > latest_observed_update_id
                        ):
                            latest_observed_update_id = update_id

                    if latest_observed_update_id is not None:
                        last_consumed_update_id = latest_observed_update_id

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

                    # Check triggers for each keyword in the list (split by |)
                    keywords = [k.strip() for k in keyword.split("|") if k.strip()]

                    # We use a custom trigger check to support multiple keywords
                    pending_updates_in_order = [
                        pending_updates_by_id[update_id]
                        for update_id in sorted(pending_updates_by_id)
                    ]

                    triggered_keyword: str | None = None
                    for triggered_candidate in keywords:
                        if dispatch_groups_for_batch(
                            pending_updates_in_order,
                            keyword=triggered_candidate,
                            chat_ids=chat_ids,
                            bot_user_id=bot_user_id,
                            bot_username=bot_username,
                        ):
                            triggered_keyword = triggered_candidate
                            break

                    grouped = None
                    if triggered_keyword:
                        grouped = dispatch_groups_for_batch(
                            pending_updates_in_order,
                            keyword=triggered_keyword,
                            chat_ids=chat_ids,
                            bot_user_id=bot_user_id,
                            bot_username=bot_username,
                        )

                        if grouped:
                            # If chat_ids is provided, treat it as an exclusive filter for dispatching
                            # to avoid duplicate processing in multi-instance setups.
                            if chat_ids is not None:
                                dispatch_groups = {
                                    cid: updates
                                    for cid, updates in grouped.items()
                                    if cid in chat_ids
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
                            if (
                                updates_store_path is not None
                                and dispatch_recent_per_chat > 0
                            ):
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
                            (
                                dispatch_groups,
                                forum_topic_created_dropped_updates,
                                forum_topic_created_dropped_groups,
                            ) = filter_dispatch_groups_without_forum_topic_created_updates(
                                dispatch_groups
                            )
                            if forum_topic_created_dropped_updates:
                                dispatch_source += "+forum_topic_created"

                            (
                                dispatch_groups,
                                cursor_dropped_updates,
                                cursor_dropped_groups,
                            ) = filter_dispatch_groups_after_last_trigger(
                                dispatch_groups,
                                last_trigger_update_id_by_chat=last_trigger_update_id_by_chat,
                            )
                            if cursor_dropped_updates:
                                dispatch_source += "+cursor"

                            capped_dropped_updates = 0
                            capped_dropped_groups = 0
                            if dispatch_recent_per_chat > 0:
                                (
                                    dispatch_groups,
                                    capped_dropped_updates,
                                    capped_dropped_groups,
                                ) = cap_dispatch_groups_per_chat(
                                    dispatch_groups,
                                    per_chat_limit=dispatch_recent_per_chat,
                                )
                                if capped_dropped_updates:
                                    dispatch_source += "+cap"

                            print(
                                "[green]AMQP trigger[/green] "
                                + f"pending={len(pending_updates_in_order)} groups={len(dispatch_groups)} "
                                + f"source={dispatch_source} replaced_groups={replaced_groups} "
                                + "forum_topic_created_dropped_updates="
                                + f"{forum_topic_created_dropped_updates} "
                                + "forum_topic_created_dropped_groups="
                                + f"{forum_topic_created_dropped_groups} "
                                + f"cursor_dropped_updates={cursor_dropped_updates} cursor_dropped_groups={cursor_dropped_groups} "
                                + f"cap_dropped_updates={capped_dropped_updates} cap_dropped_groups={capped_dropped_groups} "
                                + f"persisted={persisted if updates_store_path is not None else None}"
                            )

                            if not dispatch_groups:
                                print(
                                    "[green]AMQP dispatch[/green] "
                                    + "skipped: no updates newer than last trigger cursor"
                                )
                                pending_updates_by_id.clear()
                                continue

                            pending_updates_by_id.clear()

                            updated_cursor_chats = (
                                update_last_trigger_update_id_by_chat(
                                    last_trigger_update_id_by_chat,
                                    dispatched_groups=dispatch_groups,
                                )
                            )
                            if (
                                updated_cursor_chats
                                and trigger_cursor_state_path is not None
                            ):
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
                        else:
                            # Keep pending bounded while waiting for a trigger.
                            while len(pending_updates_by_id) > 100:
                                oldest_update_id = min(pending_updates_by_id)
                                pending_updates_by_id.pop(oldest_update_id, None)


async def run(
    *,
    token: str | None = None,
    amqp_url: str | None = None,
    queue_name: str = "telegram.messages.raw",
    keyword: str,
    model: Any | None = None,
    chat_id: str = "",
    updates_store_path: str | Path | None = None,
    dispatch_recent_per_chat: int = 0,
    timezone: str = _DEFAULT_TIMEZONE,
) -> None:
    """Entrypoint for the Telegram AMQP starter.

    Notes:
    - `updates_store_path` accepts either a `Path` or a string path and is
      normalized to a `Path` before being passed to lower-level helpers.
    - `model=None` means "use the same config-backed model as `kapy`". Callers
      may still pass an explicit `Model` instance to override that runtime.
    """
    if amqp_url is None:
        amqp_url = os.environ.get("AMQP_URL")
    if amqp_url is None:
        raise ValueError(
            "amqp_url is required (pass it directly or set AMQP_URL env var)"
        )

    config = Config()
    configure_cli_logfire(config.config_base)
    resolved_model = resolve_cli_model(config, model)
    try:
        tz = _parse_timezone(str(timezone))
    except ValueError as e:
        raise ValueError(f"Invalid timezone: {e}") from e

    parsed_chat_ids: set[int] | None
    raw_chat_ids = str(chat_id).strip()
    if not raw_chat_ids:
        parsed_chat_ids = None
    else:
        import re

        parts = [p for p in re.split(r"[,\s]+", raw_chat_ids) if p]
        try:
            parsed_chat_ids = {int(p) for p in parts}
        except ValueError as e:
            raise ValueError(f"Invalid chat_id entry in: {raw_chat_ids!r}") from e
        parsed_chat_ids = _expand_chat_id_watchlist(parsed_chat_ids)

    store_path: Path | None
    raw_store_path = (
        "" if updates_store_path is None else str(updates_store_path).strip()
    )
    if raw_store_path:
        store_path = Path(raw_store_path).expanduser()
    else:
        store_path = None

    await run_amqp_forever(
        config=config,
        model=resolved_model,
        token=token,
        amqp_url=amqp_url,
        queue_name=queue_name,
        keyword=keyword,
        chat_ids=parsed_chat_ids,
        updates_store_path=store_path,
        dispatch_recent_per_chat=dispatch_recent_per_chat,
        tz=tz,
    )

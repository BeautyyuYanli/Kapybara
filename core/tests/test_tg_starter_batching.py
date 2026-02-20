import json
import time

from k.starters.telegram import (
    filter_updates_in_time_window,
    telegram_updates_to_event,
)


def test_telegram_updates_to_event_joins_updates_by_newline() -> None:
    updates = [
        {"update_id": 1, "message": {"text": "a"}},
        {"update_id": 2, "message": {"text": "b"}},
    ]

    event = telegram_updates_to_event(updates)
    assert event.in_channel == "telegram"
    assert event.out_channel is None

    lines = event.content.splitlines()
    assert [json.loads(line)["update_id"] for line in lines] == [1, 2]


def test_telegram_updates_to_event_uses_chat_prefix_for_multi_update_batch() -> None:
    updates = [
        {
            "update_id": 1,
            "message": {
                "chat": {"id": 99, "type": "supergroup"},
                "message_thread_id": 1,
                "text": "a",
            },
        },
        {
            "update_id": 2,
            "message": {
                "chat": {"id": 99, "type": "supergroup"},
                "message_thread_id": 2,
                "text": "b",
            },
        },
    ]

    event = telegram_updates_to_event(updates)
    assert event.in_channel == "telegram/chat/99"


def test_filter_updates_in_time_window_filters_by_age_and_keeps_no_date() -> None:
    now = int(time.time())
    old = {
        "update_id": 1,
        "message": {"date": now - 11, "text": "old"},
    }
    ok = {
        "update_id": 2,
        "message": {"date": now - 1, "text": "ok"},
    }
    no_date = {
        "update_id": 3,
        "message": {"text": "no-date"},
    }

    res = filter_updates_in_time_window(
        [old, ok, no_date], now_unix_seconds=now, window_seconds=10
    )
    assert [u["update_id"] for u in res] == [2, 3]

import json
from datetime import UTC

from k.starters.telegram import telegram_update_to_event_json


def test_telegram_update_to_event_json_roundtrip() -> None:
    update = {
        "update_id": 123,
        "message": {
            "message_id": 1,
            "from": {"id": 42, "is_bot": False, "first_name": "Alice"},
            "chat": {"id": 99, "type": "private"},
            "date": 1_700_000_000,
            "text": "hello",
        },
    }

    event_json = telegram_update_to_event_json(update)
    event = json.loads(event_json)
    assert event["in_channel"] == "telegram/chat/99"
    assert event["out_channel"] is None

    body = json.loads(event["content"])
    assert body["update_id"] == 123
    assert body["message"]["message_id"] == 1
    assert body["message"]["chat"]["id"] == 99
    assert body["message"]["from"]["id"] == 42
    assert body["message"]["text"] == "hello"

    # Compacting merges/drops verbose profile fields.
    assert "first_name" not in body["message"]["from"]
    assert "last_name" not in body["message"]["from"]


def test_telegram_event_renders_message_date_in_default_timezone() -> None:
    update = {
        "update_id": 123,
        "message": {
            "message_id": 1,
            "from": {"id": 42, "first_name": "Alice"},
            "chat": {"id": 99, "type": "private"},
            "date": 1_700_000_000,
            "text": "hello",
        },
    }

    event_json = telegram_update_to_event_json(update)
    event = json.loads(event_json)
    body = json.loads(event["content"])

    assert body["message"]["date"] == "2023-11-15T06:13:20+08:00"
    assert body["message"]["date_unix"] == 1_700_000_000


def test_telegram_event_renders_message_date_in_configured_timezone() -> None:
    update = {
        "update_id": 123,
        "message": {
            "message_id": 1,
            "from": {"id": 42, "first_name": "Alice"},
            "chat": {"id": 99, "type": "private"},
            "date": 1_700_000_000,
            "text": "hello",
        },
    }

    event_json = telegram_update_to_event_json(update, tz=UTC)
    event = json.loads(event_json)
    body = json.loads(event["content"])

    assert body["message"]["date"] == "2023-11-14T22:13:20+00:00"
    assert body["message"]["date_unix"] == 1_700_000_000


def test_telegram_event_content_keeps_stage_a_chat_and_from_layout() -> None:
    update = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": 555, "first_name": "A", "last_name": "B"},
            "chat": {"id": -100123, "type": "supergroup", "title": "T"},
            "date": 1_700_000_000,
            "text": "hi",
        },
    }

    event_json = telegram_update_to_event_json(update)

    # `stage_a` matches on the JSON-escaped payload inside the event JSON.
    # Keep it simple here: ensure the `chat`/`from` objects still use the
    # familiar nested shape, with `id` as the first key.
    assert '\\"chat\\":{\\"id\\":-100123' in event_json
    assert '\\"from\\":{\\"id\\":555' in event_json


def test_telegram_event_in_channel_includes_thread_id_when_present() -> None:
    update = {
        "update_id": 7,
        "message": {
            "message_id": 70,
            "message_thread_id": 9001,
            "is_topic_message": True,
            "from": {"id": 555},
            "chat": {"id": -100123, "type": "supergroup"},
            "date": 1_700_000_000,
            "text": "thread hi",
        },
    }

    event = json.loads(telegram_update_to_event_json(update))
    assert event["in_channel"] == "telegram/chat/-100123/thread/9001"


def test_telegram_event_ignores_thread_id_when_not_topic_message() -> None:
    update = {
        "update_id": 8,
        "message": {
            "message_id": 80,
            "message_thread_id": 9002,
            "from": {"id": 555},
            "chat": {"id": -100123, "type": "supergroup"},
            "date": 1_700_000_000,
            "text": "general hi",
        },
    }

    event = json.loads(telegram_update_to_event_json(update))
    assert event["in_channel"] == "telegram/chat/-100123"


def test_telegram_event_keeps_media_file_id_but_drops_size_noise() -> None:
    update = {
        "update_id": 9,
        "message": {
            "message_id": 99,
            "from": {"id": 1, "first_name": "A"},
            "chat": {"id": 2, "type": "private"},
            "date": 1_700_000_000,
            "photo": [
                {"file_id": "small", "file_size": 1, "width": 1, "height": 1},
                {"file_id": "big", "file_size": 999, "width": 9, "height": 9},
            ],
            "document": {
                "file_id": "doc",
                "file_size": 123456,
                "file_name": "x.pdf",
                "mime_type": "application/pdf",
            },
        },
    }

    event = json.loads(telegram_update_to_event_json(update))
    body = json.loads(event["content"])

    assert body["message"]["photo"]["file_id"] == "big"
    assert "file_size" not in body["message"]["photo"]
    assert "width" not in body["message"]["photo"]
    assert "height" not in body["message"]["photo"]

    assert body["message"]["document"]["file_id"] == "doc"
    assert "file_size" not in body["message"]["document"]
    assert "file_name" not in body["message"]["document"]
    assert "mime_type" not in body["message"]["document"]

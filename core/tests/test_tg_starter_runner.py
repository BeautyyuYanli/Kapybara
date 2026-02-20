import json
from datetime import UTC

from k.starters.telegram.runner import (
    _should_compact_update_for_agent,
    _telegram_updates_to_event_text_only_compaction,
    filter_dispatch_groups_without_forum_topic_created_updates,
)


def test_should_compact_update_for_agent_for_plain_text_message() -> None:
    update = {
        "update_id": 1,
        "message": {
            "message_id": 10,
            "from": {"id": 42, "first_name": "Alice"},
            "chat": {"id": 99, "type": "private"},
            "date": 1_700_000_000,
            "text": "hello",
        },
    }
    assert _should_compact_update_for_agent(update) is True


def test_should_compact_update_for_agent_is_false_for_media_message() -> None:
    update = {
        "update_id": 2,
        "message": {
            "message_id": 11,
            "from": {"id": 43, "first_name": "Bob"},
            "chat": {"id": 99, "type": "private"},
            "date": 1_700_000_001,
            "text": "photo",
            "photo": [{"file_id": "x", "file_size": 123}],
        },
    }
    assert _should_compact_update_for_agent(update) is False


def test_should_compact_update_for_agent_is_false_for_unknown_message_shape() -> None:
    update = {
        "update_id": 3,
        "message": {
            "message_id": 12,
            "from": {"id": 43, "first_name": "Bob"},
            "chat": {"id": 99, "type": "private"},
            "date": 1_700_000_002,
            "text": "hello",
            "forward_origin": {"type": "user"},
        },
    }
    assert _should_compact_update_for_agent(update) is False


def test_should_compact_update_for_agent_is_true_for_text_entities() -> None:
    update = {
        "update_id": 4,
        "message": {
            "message_id": 13,
            "from": {"id": 44, "first_name": "Carol"},
            "chat": {"id": 99, "type": "private"},
            "date": 1_700_000_003,
            "text": "/start @bot",
            "entities": [{"offset": 0, "length": 6, "type": "bot_command"}],
        },
    }
    assert _should_compact_update_for_agent(update) is True


def test_should_compact_update_for_agent_is_true_for_reply_entities() -> None:
    update = {
        "update_id": 5,
        "message": {
            "message_id": 14,
            "from": {"id": 45, "first_name": "Dave"},
            "chat": {"id": 99, "type": "private"},
            "date": 1_700_000_004,
            "text": "ack",
            "reply_to_message": {
                "message_id": 13,
                "from": {"id": 44, "first_name": "Carol"},
                "chat": {"id": 99, "type": "private"},
                "date": 1_700_000_003,
                "edit_date": 1_700_000_004,
                "text": "/start @bot",
                "entities": [{"offset": 0, "length": 6, "type": "bot_command"}],
                "link_preview_options": {"is_disabled": True},
            },
        },
    }
    assert _should_compact_update_for_agent(update) is True


def test_telegram_updates_to_event_text_only_compacts_plain_text_only() -> None:
    updates = [
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 42, "first_name": "Alice"},
                "chat": {"id": 99, "type": "private"},
                "date": 1_700_000_000,
                "text": "hello",
                "entities": [{"offset": 0, "length": 5, "type": "bold"}],
            },
        },
        {
            "update_id": 2,
            "message": {
                "message_id": 11,
                "from": {"id": 43, "first_name": "Bob"},
                "chat": {"id": 99, "type": "private"},
                "date": 1_700_000_001,
                "text": "photo",
                "photo": [{"file_id": "x", "file_size": 123}],
            },
        },
    ]

    event = _telegram_updates_to_event_text_only_compaction(updates, tz=UTC)
    lines = event.content.splitlines()
    assert len(lines) == 2

    first = json.loads(lines[0])
    assert first["message"]["from"]["id"] == 42
    assert "first_name" not in first["message"]["from"]
    assert "entities" not in first["message"]

    second = json.loads(lines[1])
    assert second["message"]["from"]["first_name"] == "Bob"
    assert isinstance(second["message"]["photo"], list)
    assert second["message"]["photo"][0]["file_size"] == 123


def test_text_only_compaction_drops_reply_entities() -> None:
    updates = [
        {
            "update_id": 3,
            "message": {
                "message_id": 12,
                "from": {"id": 43, "first_name": "Bob"},
                "chat": {"id": 99, "type": "private"},
                "date": 1_700_000_002,
                "text": "hello",
                "reply_to_message": {
                    "message_id": 11,
                    "from": {"id": 42, "first_name": "Alice"},
                    "chat": {"id": 99, "type": "private"},
                    "date": 1_700_000_001,
                    "edit_date": 1_700_000_002,
                    "text": "https://example.com",
                    "entities": [{"offset": 0, "length": 19, "type": "url"}],
                    "link_preview_options": {"is_disabled": True},
                },
            },
        }
    ]

    event = _telegram_updates_to_event_text_only_compaction(updates, tz=UTC)
    body = json.loads(event.content)

    assert body["message"]["from"]["id"] == 43
    assert "first_name" not in body["message"]["from"]

    reply = body["message"]["reply_to_message"]
    assert reply["from"]["id"] == 42
    assert "first_name" not in reply["from"]
    assert "entities" not in reply
    assert "link_preview_options" not in reply
    assert "edit_date" not in reply


def test_text_only_compaction_drops_reply_forum_topic_created() -> None:
    updates = [
        {
            "update_id": 6,
            "message": {
                "message_id": 16,
                "from": {"id": 46, "first_name": "Eve"},
                "chat": {"id": -100123, "type": "supergroup"},
                "date": 1_700_000_006,
                "text": "topic message",
                "reply_to_message": {
                    "message_id": 15,
                    "forum_topic_created": {"name": "new topic"},
                },
            },
        }
    ]

    assert _should_compact_update_for_agent(updates[0]) is True

    event = _telegram_updates_to_event_text_only_compaction(updates, tz=UTC)
    body = json.loads(event.content)
    assert "reply_to_message" not in body["message"]


def test_text_only_compaction_keeps_topic_thread_channel() -> None:
    updates = [
        {
            "update_id": 7,
            "message": {
                "message_id": 70,
                "message_thread_id": 9001,
                "is_topic_message": True,
                "from": {"id": 555, "first_name": "A"},
                "chat": {"id": -100123, "type": "supergroup"},
                "date": 1_700_000_000,
                "text": "thread hi",
            },
        }
    ]

    event = _telegram_updates_to_event_text_only_compaction(updates, tz=UTC)
    assert event.in_channel == "telegram/chat/-100123/thread/9001"

    body = json.loads(event.content)
    assert body["message"]["from"]["id"] == 555
    assert "first_name" not in body["message"]["from"]


def test_filter_dispatch_groups_without_forum_topic_created_updates_drops_service_updates() -> (
    None
):
    groups = {
        1: [
            {
                "update_id": 1,
                "message": {
                    "chat": {"id": 1, "type": "supergroup"},
                    "forum_topic_created": {"name": "topic"},
                },
            },
            {
                "update_id": 2,
                "message": {"chat": {"id": 1, "type": "supergroup"}, "text": "normal"},
            },
        ],
        2: [
            {
                "update_id": 3,
                "message": {
                    "chat": {"id": 2, "type": "supergroup"},
                    "forum_topic_created": {"name": "topic only"},
                },
            }
        ],
    }

    filtered, dropped_updates, dropped_groups = (
        filter_dispatch_groups_without_forum_topic_created_updates(groups)
    )
    assert dropped_updates == 2
    assert dropped_groups == 1
    assert list(filtered.keys()) == [1]
    assert [u["update_id"] for u in filtered[1]] == [2]

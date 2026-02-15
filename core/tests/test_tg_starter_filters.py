from datetime import UTC

import pytest

from k.config import Config
from k.starters.telegram import (
    _expand_chat_id_watchlist,
    _poll_and_run_forever,
    chat_group_is_triggered,
    dispatch_groups_for_batch,
    extract_chat_id,
    extract_chat_type,
    group_updates_by_chat_id,
    update_is_private_chat,
    update_is_reply_to_bot,
    update_matches_keyword,
    update_mentions_bot,
)


def test_update_matches_keyword_checks_common_text_fields() -> None:
    update = {"update_id": 1, "message": {"text": "hello keyword world"}}
    assert update_matches_keyword(update, keyword="keyword") is True
    assert update_matches_keyword(update, keyword="missing") is False


def test_update_matches_keyword_is_case_insensitive() -> None:
    update = {"update_id": 1, "message": {"text": "Hello WORLD"}}
    assert update_matches_keyword(update, keyword="world") is True


def test_update_matches_keyword_falls_back_to_json() -> None:
    update = {"update_id": 1, "custom": {"payload": "needle"}}
    assert update_matches_keyword(update, keyword="needle") is True


def test_extract_chat_id() -> None:
    assert extract_chat_id({"message": {"chat": {"id": 123}}}) == 123
    assert (
        extract_chat_id({"callback_query": {"message": {"chat": {"id": -99}}}}) == -99
    )
    assert extract_chat_id({"update_id": 1}) is None


def test_extract_chat_type_and_private_detection() -> None:
    update = {"message": {"chat": {"id": 1, "type": "private"}, "text": "hi"}}
    assert extract_chat_type(update) == "private"
    assert update_is_private_chat(update) is True


def test_update_mentions_bot() -> None:
    update = {"message": {"chat": {"id": 1, "type": "group"}, "text": "hi @MyBot"}}
    assert update_mentions_bot(update, bot_username="MyBot") is True
    assert update_mentions_bot(update, bot_username="OtherBot") is False


def test_update_is_reply_to_bot_by_id_or_username() -> None:
    update = {
        "message": {
            "chat": {"id": 1, "type": "group"},
            "text": "reply",
            "reply_to_message": {"from": {"id": 123, "username": "MyBot"}},
        }
    }
    assert update_is_reply_to_bot(update, bot_user_id=123, bot_username=None) is True
    assert (
        update_is_reply_to_bot(update, bot_user_id=None, bot_username="MyBot") is True
    )
    assert (
        update_is_reply_to_bot(update, bot_user_id=999, bot_username="Other") is False
    )


def test_chat_group_is_triggered_by_reply() -> None:
    updates = [
        {"message": {"chat": {"id": 1, "type": "group"}, "text": "hello"}},
        {
            "message": {
                "chat": {"id": 1, "type": "group"},
                "text": "hi",
                "reply_to_message": {"from": {"id": 123, "username": "MyBot"}},
            }
        },
    ]
    assert (
        chat_group_is_triggered(
            updates,
            keyword="nope",
            bot_user_id=123,
            bot_username="MyBot",
        )
        is True
    )


def test_dispatch_groups_for_batch_dispatches_all_groups_when_any_triggers() -> None:
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 1, "type": "group"}, "text": "hi"}},
        {
            "update_id": 2,
            "message": {
                "chat": {"id": 2, "type": "group"},
                "text": "ping @MyBot",
            },
        },
    ]
    grouped = dispatch_groups_for_batch(
        updates,
        keyword="nope",
        chat_ids=None,
        bot_user_id=123,
        bot_username="MyBot",
    )
    assert grouped is not None
    assert sorted(grouped.keys()) == [1, 2]


def test_dispatch_groups_for_batch_trigger_watchlist_includes_other_chats_on_dispatch() -> (
    None
):
    updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 1, "type": "group"}, "text": "kapy hi"},
        },
        {
            "update_id": 2,
            "message": {"chat": {"id": 2, "type": "group"}, "text": "untriggered"},
        },
    ]
    grouped = dispatch_groups_for_batch(
        updates,
        keyword="kapy",
        chat_ids={1},
        bot_user_id=None,
        bot_username=None,
    )
    assert grouped is not None
    assert sorted(grouped.keys()) == [1, 2]


def test_dispatch_groups_for_batch_trigger_watchlist_requires_trigger_in_watchlist() -> (
    None
):
    updates = [
        {
            "update_id": 1,
            "message": {"chat": {"id": 2, "type": "group"}, "text": "kapy hi"},
        },
    ]
    assert (
        dispatch_groups_for_batch(
            updates,
            keyword="kapy",
            chat_ids={1},
            bot_user_id=None,
            bot_username=None,
        )
        is None
    )


def test_dispatch_groups_for_batch_returns_none_when_no_trigger() -> None:
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 1, "type": "group"}, "text": "hi"}},
        {"update_id": 2, "message": {"chat": {"id": 2, "type": "group"}, "text": "yo"}},
    ]
    assert (
        dispatch_groups_for_batch(
            updates,
            keyword="nope",
            chat_ids=None,
            bot_user_id=123,
            bot_username="MyBot",
        )
        is None
    )


def test_expand_chat_id_watchlist_adds_supergroup_variants() -> None:
    assert _expand_chat_id_watchlist({-1886218691}) == {-1886218691, -1001886218691}
    assert _expand_chat_id_watchlist({-1001886218691}) == {-1886218691, -1001886218691}


def test_group_updates_by_chat_id_groups_and_keeps_unknown_when_no_allowlist() -> None:
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 1}, "text": "a"}},
        {"update_id": 2, "message": {"chat": {"id": 2}, "text": "b"}},
        {"update_id": 3, "update": {"no_chat": True}},
    ]
    grouped = group_updates_by_chat_id(updates, chat_ids=None)
    assert sorted(grouped.keys(), key=lambda k: (k is None, k)) == [1, 2, None]
    assert [u["update_id"] for u in grouped[1]] == [1]
    assert [u["update_id"] for u in grouped[2]] == [2]
    assert [u["update_id"] for u in grouped[None]] == [3]


def test_group_updates_by_chat_id_filters_by_allowlist() -> None:
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 1}, "text": "a"}},
        {"update_id": 2, "message": {"chat": {"id": 2}, "text": "b"}},
        {"update_id": 3, "update": {"no_chat": True}},
    ]
    grouped = group_updates_by_chat_id(updates, chat_ids={2})
    assert list(grouped.keys()) == [2]
    assert [u["update_id"] for u in grouped[2]] == [2]


@pytest.mark.anyio
async def test_poll_and_run_forever_requires_keyword(tmp_path) -> None:
    cfg = Config(fs_base=tmp_path)
    with pytest.raises(ValueError, match="--keyword"):
        await _poll_and_run_forever(
            config=cfg,
            model_name="openai/gpt-5.2",
            token="test-token",
            timeout_seconds=1,
            keyword=" ",
            time_window_seconds=60,
            chat_ids=None,
            tz=UTC,
        )

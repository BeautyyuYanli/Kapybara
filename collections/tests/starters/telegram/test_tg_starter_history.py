from pathlib import Path

import pytest

from kapy_collections.starters.telegram import (
    append_updates_jsonl,
    load_last_trigger_update_id_by_chat,
    load_recent_updates_grouped_by_chat_id,
    save_last_trigger_update_id_by_chat,
    trigger_cursor_state_path_for_updates_store,
)
from kapy_collections.starters.telegram.runner import (
    cap_dispatch_groups_per_chat,
    filter_dispatch_groups_after_last_trigger,
    overlay_dispatch_groups_with_recent,
    update_last_trigger_update_id_by_chat,
)


def test_append_and_load_recent_updates_grouped_by_chat_id(tmp_path) -> None:
    path = tmp_path / "telegram" / "updates.jsonl"
    updates = [
        {"update_id": 1, "message": {"chat": {"id": 1}, "text": "a"}},
        {"update_id": 2, "message": {"chat": {"id": 2}, "text": "b"}},
        {"update_id": 3, "message": {"chat": {"id": 1}, "text": "c"}},
        {"update_id": 4, "message": {"chat": {"id": 1}, "text": "d"}},
        {
            "update_id": 5,
            "callback_query": {"message": {"chat": {"id": 2}}, "data": "x"},
        },
        {"update_id": 6, "custom": {"payload": "no-chat"}},
    ]

    assert append_updates_jsonl(path, updates) == len(updates)

    grouped = load_recent_updates_grouped_by_chat_id(path, per_chat_limit=2)
    assert [u["update_id"] for u in grouped[1]] == [3, 4]
    assert [u["update_id"] for u in grouped[2]] == [2, 5]
    assert [u["update_id"] for u in grouped[None]] == [6]


def test_load_recent_updates_grouped_by_chat_id_missing_file_returns_empty(
    tmp_path,
) -> None:
    path = tmp_path / "missing" / "updates.jsonl"
    assert load_recent_updates_grouped_by_chat_id(path, per_chat_limit=3) == {}


def test_load_recent_updates_grouped_by_chat_id_rejects_invalid_limit(tmp_path) -> None:
    path = tmp_path / "updates.jsonl"
    with pytest.raises(ValueError, match="per_chat_limit"):
        load_recent_updates_grouped_by_chat_id(path, per_chat_limit=0)


def test_load_recent_updates_grouped_by_chat_id_rejects_invalid_json(tmp_path) -> None:
    path = tmp_path / "updates.jsonl"
    path.write_text('{"update_id":1}\nnot-json\n', encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid JSON"):
        load_recent_updates_grouped_by_chat_id(path, per_chat_limit=2)


def test_overlay_dispatch_groups_with_recent_keeps_pending_chat_boundary() -> None:
    pending = {
        1: [{"update_id": 10, "message": {"chat": {"id": 1}, "text": "pending-a"}}],
        2: [{"update_id": 20, "message": {"chat": {"id": 2}, "text": "pending-b"}}],
    }
    recent = {
        1: [{"update_id": 100, "message": {"chat": {"id": 1}, "text": "recent-a"}}],
        3: [{"update_id": 300, "message": {"chat": {"id": 3}, "text": "recent-c"}}],
    }

    selected, replaced = overlay_dispatch_groups_with_recent(
        pending,
        recent_groups=recent,
    )

    assert replaced == 1
    assert list(selected.keys()) == [1, 2]
    assert [u["update_id"] for u in selected[1]] == [100]
    assert [u["update_id"] for u in selected[2]] == [20]


def test_trigger_cursor_state_path_for_updates_store() -> None:
    path = trigger_cursor_state_path_for_updates_store(
        Path("/tmp/telegram/updates.jsonl")
    )
    assert str(path).endswith("updates.jsonl.trigger_cursor_state.json")

    path_from_str = trigger_cursor_state_path_for_updates_store(
        "/tmp/telegram/updates.jsonl"
    )
    assert path_from_str == path


def test_save_and_load_last_trigger_update_id_by_chat_roundtrip(tmp_path) -> None:
    path = tmp_path / "telegram" / "state.json"
    original = {1: 10, -1002: 99}

    save_last_trigger_update_id_by_chat(path, original)
    loaded = load_last_trigger_update_id_by_chat(path)
    assert loaded == original


def test_load_last_trigger_update_id_by_chat_missing_file_returns_empty(
    tmp_path,
) -> None:
    path = tmp_path / "telegram" / "state.json"
    assert load_last_trigger_update_id_by_chat(path) == {}


def test_load_last_trigger_update_id_by_chat_rejects_invalid_schema(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"version":1,"last_trigger_update_id_by_chat":{"1":"x"}}')

    with pytest.raises(ValueError, match="invalid update id"):
        load_last_trigger_update_id_by_chat(path)


def test_filter_dispatch_groups_after_last_trigger() -> None:
    dispatch_groups = {
        1: [
            {"update_id": 10, "message": {"chat": {"id": 1}}},
            {"update_id": 11, "message": {"chat": {"id": 1}}},
            {"message": {"chat": {"id": 1}, "text": "missing-id"}},
        ],
        2: [{"update_id": 5, "message": {"chat": {"id": 2}}}],
        None: [{"update_id": 7, "custom": {"no_chat": True}}],
    }

    filtered, dropped_updates, dropped_groups = (
        filter_dispatch_groups_after_last_trigger(
            dispatch_groups,
            last_trigger_update_id_by_chat={1: 10},
        )
    )

    assert dropped_updates == 2
    assert dropped_groups == 0
    assert [u["update_id"] for u in filtered[1]] == [11]
    assert [u["update_id"] for u in filtered[2]] == [5]
    assert [u["update_id"] for u in filtered[None]] == [7]


def test_filter_dispatch_groups_after_last_trigger_can_drop_whole_group() -> None:
    dispatch_groups = {
        1: [{"update_id": 9, "message": {"chat": {"id": 1}}}],
    }
    filtered, dropped_updates, dropped_groups = (
        filter_dispatch_groups_after_last_trigger(
            dispatch_groups,
            last_trigger_update_id_by_chat={1: 10},
        )
    )

    assert filtered == {}
    assert dropped_updates == 1
    assert dropped_groups == 1


def test_update_last_trigger_update_id_by_chat_advances_max_per_chat() -> None:
    state = {1: 10}
    dispatched = {
        1: [
            {"update_id": 11, "message": {"chat": {"id": 1}}},
            {"update_id": 15, "message": {"chat": {"id": 1}}},
        ],
        2: [
            {"message": {"chat": {"id": 2}, "text": "missing-id"}},
            {"update_id": 7, "message": {"chat": {"id": 2}}},
        ],
        None: [{"update_id": 100}],
    }

    updated_chats = update_last_trigger_update_id_by_chat(
        state,
        dispatched_groups=dispatched,
    )

    assert updated_chats == 2
    assert state == {1: 15, 2: 7}


def test_cap_dispatch_groups_per_chat_keeps_latest_updates() -> None:
    dispatch_groups = {
        1: [
            {"update_id": 1, "message": {"chat": {"id": 1}}},
            {"update_id": 2, "message": {"chat": {"id": 1}}},
            {"update_id": 3, "message": {"chat": {"id": 1}}},
        ],
        2: [
            {"update_id": 10, "message": {"chat": {"id": 2}}},
        ],
    }

    capped, dropped_updates, capped_groups = cap_dispatch_groups_per_chat(
        dispatch_groups,
        per_chat_limit=2,
    )

    assert [u["update_id"] for u in capped[1]] == [2, 3]
    assert [u["update_id"] for u in capped[2]] == [10]
    assert dropped_updates == 1
    assert capped_groups == 1


def test_cap_dispatch_groups_per_chat_rejects_non_positive_limit() -> None:
    with pytest.raises(ValueError, match="per_chat_limit"):
        cap_dispatch_groups_per_chat(
            {1: [{"update_id": 1, "message": {"chat": {"id": 1}}}]},
            per_chat_limit=0,
        )

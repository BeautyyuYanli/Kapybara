from kapy_collections.starters.telegram import (
    extract_update_id,
    filter_unseen_updates,
)


def test_extract_update_id() -> None:
    assert extract_update_id({"update_id": 1}) == 1
    assert extract_update_id({"update_id": "1"}) is None
    assert extract_update_id({}) is None


def test_filter_unseen_updates_skips_processed_and_duplicates() -> None:
    updates = [
        {"update_id": 9, "message": {"text": "old"}},
        {"update_id": 10, "message": {"text": "new-1"}},
        {"update_id": 10, "message": {"text": "dup"}},
        {"update_id": 11, "message": {"text": "new-2"}},
        {"update_id": "12", "message": {"text": "bad-id"}},
    ]

    res = filter_unseen_updates(updates, last_processed_update_id=9)
    assert [u["update_id"] for u in res] == [10, 11]

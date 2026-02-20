from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai import ModelRetry, RunContext

from k.agent.core.entities import finish_action
from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore


def _ctx_for_store(store: FolderMemoryStore) -> RunContext[Any]:
    return cast(
        RunContext[Any],
        SimpleNamespace(deps=SimpleNamespace(memory_storage=store)),
    )


def test_finish_action_accepts_existing_referenced_memory_ids(tmp_path) -> None:
    store = FolderMemoryStore(tmp_path / "memories")
    parent = MemoryRecord(in_channel="telegram/chat/1", input="in", output="out")
    store.append(parent)

    result = finish_action(
        _ctx_for_store(store),
        referenced_memory_ids=[parent.id_],
        from_where_and_response_to_where="test",
        user_intents="test",
    )

    assert result.referenced_memory_ids == [parent.id_]


def test_finish_action_retries_for_invalid_memory_id(tmp_path) -> None:
    store = FolderMemoryStore(tmp_path / "memories")

    with pytest.raises(ModelRetry, match="Invalid referenced_memory_ids"):
        finish_action(
            _ctx_for_store(store),
            referenced_memory_ids=["not-a-memory-id"],
            from_where_and_response_to_where="test",
            user_intents="test",
        )


def test_finish_action_retries_for_missing_memory_id(tmp_path) -> None:
    store = FolderMemoryStore(tmp_path / "memories")
    missing_id = MemoryRecord(
        in_channel="telegram/chat/1", input="in", output="out"
    ).id_

    with pytest.raises(ModelRetry, match="Unknown referenced_memory_ids"):
        finish_action(
            _ctx_for_store(store),
            referenced_memory_ids=[missing_id],
            from_where_and_response_to_where="test",
            user_intents="test",
        )

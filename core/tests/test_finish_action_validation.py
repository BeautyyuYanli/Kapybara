from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic_ai import ModelRetry, RunContext

from k.agent.core.agent import finish_action
from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore


def _ctx_for_store(
    store: FolderMemoryStore,
    *,
    config_base,
    contacts: list[str] | None = None,
) -> RunContext[Any]:
    event = SimpleNamespace(
        in_channel="telegram/chat/1",
        out_channel=None,
        contacts=contacts or [],
    )
    return cast(
        RunContext[Any],
        SimpleNamespace(
            deps=SimpleNamespace(
                memory_storage=store,
                start_event=event,
                config=SimpleNamespace(config_base=config_base),
            ),
        ),
    )


def test_finish_action_accepts_existing_referenced_memory_ids(tmp_path) -> None:
    store = FolderMemoryStore(tmp_path / "memories")
    parent = MemoryRecord(in_channel="telegram/chat/1", input="in", output="out")
    store.append(parent)

    result = finish_action(
        _ctx_for_store(
            store,
            config_base=tmp_path / ".kapybara",
            contacts=["telegram/42"],
        ),
        referenced_memory_ids=[parent.id_],
        raw_input="user asked for test output",
        raw_output="agent replied with final answer",
        input_intents="1. verify memory references",
        compacted_actions=["Received request -> Tried validation -> Observed success"],
    )

    assert result.parents == [parent.id_]
    assert result.output == "agent replied with final answer"
    assert result.compacted[0] == "<input>user asked for test output</input>"
    assert result.compacted[1] == "<intents>1. verify memory references</intents>"
    assert (
        result.compacted[2]
        == "Received request -> Tried validation -> Observed success"
    )
    assert result.compacted[3] == "<output>agent replied with final answer</output>"
    assert len(result.contacts) == 1
    contacts_book = json.loads((tmp_path / ".kapybara" / "contacts.json").read_text())
    assert contacts_book[result.contacts[0]] == ["telegram/42"]


def test_finish_action_retries_for_invalid_memory_id(tmp_path) -> None:
    store = FolderMemoryStore(tmp_path / "memories")

    with pytest.raises(ModelRetry, match="Invalid referenced_memory_ids"):
        finish_action(
            _ctx_for_store(store, config_base=tmp_path / ".kapybara"),
            referenced_memory_ids=["not-a-memory-id"],
            raw_input="user asked for test output",
            raw_output="agent replied with final answer",
            input_intents="1. verify memory references",
            compacted_actions=[
                "Received request -> Tried validation -> Observed failure"
            ],
        )


def test_finish_action_ignores_missing_memory_id(tmp_path) -> None:
    store = FolderMemoryStore(tmp_path / "memories")
    missing_id = MemoryRecord(
        in_channel="telegram/chat/1", input="in", output="out"
    ).id_

    result = finish_action(
        _ctx_for_store(store, config_base=tmp_path / ".kapybara"),
        referenced_memory_ids=[missing_id],
        raw_input="user asked for test output",
        raw_output="agent replied with final answer",
        input_intents="1. verify memory references",
        compacted_actions=[
            "Received request -> Tried validation -> Observed missing memory"
        ],
    )

    assert result.parents == []

from __future__ import annotations

import importlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from k.agent.core.agent import agent, agent_run
from k.agent.core.entities import Event
from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config

agent_module = importlib.import_module("k.agent.core.agent")


@dataclass(slots=True)
class _FakeRunResult:
    output: MemoryRecord
    _messages: list[ModelRequest | ModelResponse]

    def new_messages(self) -> list[ModelRequest | ModelResponse]:
        return list(self._messages)


@pytest.mark.anyio
async def test_agent_run_returns_compacted_memory_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured_user_prompt: tuple[Any, ...] | None = None
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_view_base = tmp_path / "agent-view" / ".kapybara"
    monkeypatch.setenv("K_CONFIG_BASE", str(agent_view_base))
    pref_path = tmp_path / ".kapybara" / "preferences" / "test.md"
    pref_path.parent.mkdir(parents=True, exist_ok=True)
    pref_path.write_text("test channel preference", encoding="utf-8")

    async def fake_agent_config_base_value(**kwargs: Any) -> str:
        _ = kwargs
        return str(agent_view_base)

    async def fake_agent_run(**kwargs: Any) -> _FakeRunResult:
        nonlocal captured_user_prompt
        user_prompt = kwargs.get("user_prompt")
        if isinstance(user_prompt, tuple):
            captured_user_prompt = user_prompt
        messages: list[ModelRequest | ModelResponse] = [
            ModelRequest(parts=[UserPromptPart(content=("old prompt",))]),
            ModelResponse(parts=[TextPart(content="assistant did a thing")]),
            ModelResponse(parts=[TextPart(content="finish_action")]),
        ]
        return _FakeRunResult(
            output=MemoryRecord(
                in_channel="test",
                input="",
                compacted=["compacted-step"],
            ),
            _messages=messages,
        )

    monkeypatch.setattr(agent, "run", fake_agent_run)
    monkeypatch.setattr(
        agent_module, "agent_config_base_value", fake_agent_config_base_value
    )

    config = Config(config_base=tmp_path / ".kapybara")
    memory_store = FolderMemoryStore(config.config_base / "memories")

    mem = await agent_run(
        model="test-model",
        config=config,
        memory_store=memory_store,
        instruct=Event(
            in_channel="test",
            contacts=["test/system"],
            content="do something",
        ),
        parent_memories=[],
    )

    assert mem.compacted == ["compacted-step"]
    assert mem.input == "do something"
    assert mem.in_channel == "test"

    assert captured_user_prompt is not None
    system_prompt = captured_user_prompt[1]
    assert isinstance(system_prompt, str)
    assert system_prompt.startswith("<System>")
    assert (
        f"Value of `${{K_CONFIG_BASE:-~/.kapybara}}`: {agent_view_base}"
        in system_prompt
    )
    assert captured_user_prompt[0] == ""
    assert captured_user_prompt[3] == "do something"
    assert all(
        not (isinstance(part, str) and part.startswith("<Preferences>"))
        for part in captured_user_prompt
    )
    assert f"Path: {pref_path}" not in "".join(
        part for part in captured_user_prompt if isinstance(part, str)
    )
    event_meta = captured_user_prompt[2]
    assert isinstance(event_meta, str)
    assert event_meta.startswith("<EventMeta>")
    assert '"in_channel":"test"' in event_meta
    assert '"contacts":["test/system"]' in event_meta
    assert '"content"' not in event_meta


def test_resolve_parent_memories_none_unions_channel_and_contacts_with_dedupe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FolderMemoryStore(tmp_path / "memories")
    calls: list[tuple[str | None, str | None, int | None]] = []

    def fake_get_latests(
        *,
        in_channel: str | None = None,
        contact: str | None = None,
        num: int | None = None,
    ) -> list[str]:
        calls.append((in_channel, contact, num))
        result: list[str]
        if in_channel == "telegram/chat/1":
            result = ["m5", "m4", "m3", "m2", "m1", "m0"]
        elif contact == "c1":
            result = ["c3", "m5", "c2", "c1"]
        elif contact == "c2":
            result = ["c4", "m4", "c2"]
        else:
            result = []
        return result if num is None else result[:num]

    monkeypatch.setattr(store, "get_latests", fake_get_latests)

    selected = agent_module._resolve_parent_memories(
        memory_store=store,
        in_channel="telegram/chat/1",
        contacts=["c1", "c2"],
        parent_memories=None,
    )

    assert selected == ["m5", "m4", "m3", "m2", "m1", "c3", "c4"]
    assert calls == [
        ("telegram/chat/1", None, 5),
        (None, "c1", 2),
        (None, "c2", 2),
    ]


def test_resolve_parent_memories_explicit_empty_skips_auto_selection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FolderMemoryStore(tmp_path / "memories")
    calls: list[tuple[str | None, str | None, int | None]] = []

    def fake_get_latests(
        *,
        in_channel: str | None = None,
        contact: str | None = None,
        num: int | None = None,
    ) -> list[str]:
        calls.append((in_channel, contact, num))
        return ["m1"]

    monkeypatch.setattr(store, "get_latests", fake_get_latests)

    selected = agent_module._resolve_parent_memories(
        memory_store=store,
        in_channel="telegram/chat/1",
        contacts=["c1"],
        parent_memories=[],
    )

    assert selected == []
    assert calls == []


def test_resolve_parent_memories_explicit_list_is_deduped(
    tmp_path: Path,
) -> None:
    store = FolderMemoryStore(tmp_path / "memories")
    selected = agent_module._resolve_parent_memories(
        memory_store=store,
        in_channel="telegram/chat/1",
        contacts=["c1"],
        parent_memories=["a", "b", "a", "c", "b"],
    )
    assert selected == ["a", "b", "c"]


def test_memory_select_default_levels_are_3_and_10() -> None:
    assert agent_module._memory_select.__defaults__ == (3, 10, 40, 20)


@pytest.mark.anyio
async def test_memory_select_downgrades_exceeded_recent_before_raw_pair_cap(
    tmp_path: Path,
) -> None:
    store = FolderMemoryStore(tmp_path / "memories")

    m1 = MemoryRecord(
        in_channel="test",
        input="m1",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 1),
    )
    m2 = MemoryRecord(
        in_channel="test",
        input="m2",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 2),
        parents=[m1.id_],
    )
    m3 = MemoryRecord(
        in_channel="test",
        input="m3",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 3),
        parents=[m2.id_],
    )
    m4 = MemoryRecord(
        in_channel="test",
        input="m4",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 4),
        parents=[m3.id_],
    )
    m5 = MemoryRecord(
        in_channel="test",
        input="m5",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 5),
        parents=[m4.id_],
    )
    for rec in (m1, m2, m3, m4, m5):
        store.append(rec)

    all_mem_rec, recent_mem = await agent_module._memory_select(
        store,
        [m5.id_],
        compacted_level_num=1,
        raw_pair_level_num=3,
        compacted_cap_num=1,
        raw_pair_cap_num=1,
    )

    assert recent_mem == {m5.id_}
    assert [rec.id_ for rec in all_mem_rec] == [m4.id_, m5.id_]


@pytest.mark.anyio
async def test_memory_select_downgraded_recent_can_fill_raw_pair_slot(
    tmp_path: Path,
) -> None:
    store = FolderMemoryStore(tmp_path / "memories")

    older = MemoryRecord(
        in_channel="test",
        input="older",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 1),
    )
    newer = MemoryRecord(
        in_channel="test",
        input="newer",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 2),
        parents=[older.id_],
    )
    for rec in (older, newer):
        store.append(rec)

    all_mem_rec, recent_mem = await agent_module._memory_select(
        store,
        [newer.id_],
        compacted_level_num=1,
        raw_pair_level_num=1,
        compacted_cap_num=1,
        raw_pair_cap_num=1,
    )

    assert recent_mem == {newer.id_}
    assert [rec.id_ for rec in all_mem_rec] == [older.id_, newer.id_]


@pytest.mark.anyio
async def test_memory_select_rejects_negative_caps(tmp_path: Path) -> None:
    store = FolderMemoryStore(tmp_path / "memories")

    with pytest.raises(ValueError, match="compacted_cap_num must be >= 0"):
        await agent_module._memory_select(
            store,
            [],
            compacted_cap_num=-1,
        )
    with pytest.raises(ValueError, match="raw_pair_cap_num must be >= 0"):
        await agent_module._memory_select(
            store,
            [],
            raw_pair_cap_num=-1,
        )

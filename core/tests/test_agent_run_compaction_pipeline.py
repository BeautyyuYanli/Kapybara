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
from k.agent.memory.retrieval.by_contact import (
    latest_memory_roots_by_contact,
    select_memory_ids_by_contact,
)
from k.agent.memory.retrieval.by_in_channel import (
    latest_memory_roots_by_in_channel,
    select_memory_ids_by_in_channel,
)
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
    captured_run_metadata: dict[str, Any] | None = None
    captured_usage_limits: Any = None
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
        nonlocal captured_run_metadata
        nonlocal captured_usage_limits
        user_prompt = kwargs.get("user_prompt")
        if isinstance(user_prompt, tuple):
            captured_user_prompt = user_prompt
        maybe_metadata = kwargs.get("metadata")
        if isinstance(maybe_metadata, dict):
            captured_run_metadata = maybe_metadata
        captured_usage_limits = kwargs.get("usage_limits")
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
    assert len(captured_user_prompt) == 3
    system_prompt = captured_user_prompt[0]
    assert isinstance(system_prompt, str)
    assert system_prompt.startswith("<System>")
    assert (
        f"Value of `${{K_CONFIG_BASE:-~/.kapybara}}`: {agent_view_base}"
        in system_prompt
    )
    assert captured_user_prompt[2] == "do something"
    assert all(
        not (isinstance(part, str) and part.startswith("<Memories>"))
        for part in captured_user_prompt
    )
    assert all(
        not (isinstance(part, str) and part.startswith("<Preferences>"))
        for part in captured_user_prompt
    )
    assert f"Path: {pref_path}" not in "".join(
        part for part in captured_user_prompt if isinstance(part, str)
    )
    event_meta = captured_user_prompt[1]
    assert isinstance(event_meta, str)
    assert event_meta.startswith("<EventMeta>")
    assert '"in_channel":"test"' in event_meta
    assert '"contacts":["test/system"]' in event_meta
    assert '"content"' not in event_meta
    assert captured_usage_limits is not None
    assert captured_run_metadata is not None
    assert captured_run_metadata["request_limit"] == captured_usage_limits.request_limit


@pytest.mark.anyio
async def test_agent_run_injects_explicit_parent_memories_without_store_lookup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured_user_prompt: tuple[Any, ...] | None = None
    logged: list[tuple[str, tuple[Any, ...]]] = []
    monkeypatch.setenv("HOME", str(tmp_path))
    agent_view_base = tmp_path / "agent-view" / ".kapybara"
    monkeypatch.setenv("K_CONFIG_BASE", str(agent_view_base))

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

    def fake_log_info(msg: str, *args: Any, **kwargs: Any) -> None:
        _ = kwargs
        logged.append((msg, args))

    monkeypatch.setattr(agent_module.logger, "info", fake_log_info)

    config = Config(config_base=tmp_path / ".kapybara")
    memory_store = FolderMemoryStore(config.config_base / "memories")

    def fail_get_latests(**kwargs: Any) -> list[str]:
        _ = kwargs
        raise AssertionError("explicit parent_memories should not query latest records")

    def fail_get_ancestors(*args: Any, **kwargs: Any) -> list[str]:
        _ = (args, kwargs)
        raise AssertionError("explicit parent_memories should not query ancestors")

    monkeypatch.setattr(memory_store, "get_latests", fail_get_latests)
    monkeypatch.setattr(memory_store, "get_ancestors", fail_get_ancestors)

    await agent_run(
        model="test-model",
        config=config,
        memory_store=memory_store,
        instruct=Event(
            in_channel="test",
            contacts=["test/system"],
            content="do something",
        ),
        parent_memories=["memory-a", "memory-b"],
    )

    assert captured_user_prompt is not None
    assert len(captured_user_prompt) == 3
    assert isinstance(captured_user_prompt[0], str)
    assert captured_user_prompt[0].startswith("<System>")
    assert captured_user_prompt[2] == "do something"
    assert all(
        not (isinstance(part, str) and part.startswith("<Memories>"))
        for part in captured_user_prompt
    )
    assert (
        "Injected memories counts (explicit): explicit=%d, injected_total=%d",
        (2, 2),
    ) in logged


def test_latest_memory_roots_by_in_channel_collects_channel_ids(
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

    channel_roots = latest_memory_roots_by_in_channel(
        store,
        in_channel="telegram/chat/1",
    )

    assert channel_roots == ["m5", "m4", "m3", "m2", "m1"]
    assert calls == [("telegram/chat/1", None, 5)]


def test_latest_memory_roots_by_contact_dedupes_with_stable_order(
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
        if in_channel == "telegram/chat/1":
            return ["m3", "m2", "m3", "m1"]
        if contact == "c1":
            return ["m2", "c1"]
        if contact == "c2":
            return ["c1", "c2"]
        return []

    monkeypatch.setattr(store, "get_latests", fake_get_latests)

    contact_roots = latest_memory_roots_by_contact(
        store,
        contacts=["c1", "c2"],
    )

    assert contact_roots == ["m2", "c1", "c2"]
    assert calls == [
        (None, "c1", 1),
        (None, "c2", 1),
    ]


def test_select_memory_ids_by_in_channel_default_levels_and_caps() -> None:
    assert select_memory_ids_by_in_channel.__kwdefaults__ == {
        "roots": None,
        "latest_num": 5,
        "compacted_level_num": 1,
        "raw_pair_level_num": 3,
        "compacted_cap_num": 15,
        "raw_pair_cap_num": 15,
    }


def test_select_memory_ids_by_contact_default_levels_and_caps() -> None:
    assert select_memory_ids_by_contact.__kwdefaults__ == {
        "roots": None,
        "latest_num": 1,
        "compacted_level_num": 0,
        "raw_pair_level_num": 1,
        "compacted_cap_num": 5,
        "raw_pair_cap_num": 5,
    }


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

    compacted_ids, raw_pair_ids = select_memory_ids_by_in_channel(
        store,
        in_channel="test",
        roots=[m5.id_],
        compacted_level_num=1,
        raw_pair_level_num=3,
        compacted_cap_num=1,
        raw_pair_cap_num=1,
    )

    assert compacted_ids == [m5.id_]
    assert raw_pair_ids == [m4.id_]


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

    compacted_ids, raw_pair_ids = select_memory_ids_by_in_channel(
        store,
        in_channel="test",
        roots=[newer.id_],
        compacted_level_num=1,
        raw_pair_level_num=1,
        compacted_cap_num=1,
        raw_pair_cap_num=1,
    )

    assert compacted_ids == [newer.id_]
    assert raw_pair_ids == [older.id_]


@pytest.mark.anyio
async def test_memory_select_contact_roots_expand_raw_pair_only(
    tmp_path: Path,
) -> None:
    store = FolderMemoryStore(tmp_path / "memories")

    older = MemoryRecord(
        in_channel="other",
        contacts=["c1"],
        input="older",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 1),
    )
    newer = MemoryRecord(
        in_channel="other",
        contacts=["c1"],
        input="newer",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 2),
        parents=[older.id_],
    )
    for rec in (older, newer):
        store.append(rec)

    compacted_ids, raw_pair_ids = select_memory_ids_by_contact(
        store,
        contacts=["c1"],
        roots=[newer.id_],
        compacted_level_num=0,
        raw_pair_level_num=1,
    )

    assert compacted_ids == [newer.id_]
    assert raw_pair_ids == [older.id_]


@pytest.mark.anyio
async def test_memory_select_logs_injected_category_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    store = FolderMemoryStore(tmp_path / "memories")
    logged: list[tuple[str, tuple[Any, ...]]] = []

    channel_older = MemoryRecord(
        in_channel="channel",
        input="channel_older",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 1),
    )
    channel_newer = MemoryRecord(
        in_channel="channel",
        input="channel_newer",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 2),
        parents=[channel_older.id_],
    )
    contact_newer = MemoryRecord(
        in_channel="contact",
        contacts=["c1"],
        input="contact_newer",
        output="",
        created_at=datetime(2026, 1, 1, 0, 0, 3),
        # Cross-scope link: contact raw-pair selects channel_newer, but it is
        # later removed from injected raw-pair because channel compacted keeps it.
        parents=[channel_newer.id_],
    )
    for rec in (channel_older, channel_newer, contact_newer):
        store.append(rec)

    def fake_log_info(msg: str, *args: Any, **kwargs: Any) -> None:
        _ = kwargs
        logged.append((msg, args))

    monkeypatch.setattr(agent_module.logger, "info", fake_log_info)

    all_mem_rec, recent_mem, memory_parent_ids = (
        agent_module._select_auto_memory_records(
            store,
            in_channel="channel",
            contacts=["c1"],
        )
    )

    assert recent_mem == {channel_older.id_, channel_newer.id_, contact_newer.id_}
    assert memory_parent_ids == [
        channel_newer.id_,
        channel_older.id_,
        contact_newer.id_,
    ]
    assert [rec.id_ for rec in all_mem_rec] == [
        channel_older.id_,
        channel_newer.id_,
        contact_newer.id_,
    ]
    assert logged == [
        (
            "Injected memories counts (auto): "
            "channel_compacted_selected=%d, channel_raw_pair_selected=%d, "
            "contact_compacted_selected=%d, contact_raw_pair_selected=%d, "
            "injected_compacted=%d, injected_raw_pair=%d, injected_total=%d",
            (2, 0, 1, 1, 3, 0, 3),
        )
    ]


@pytest.mark.anyio
async def test_memory_select_rejects_negative_caps(tmp_path: Path) -> None:
    store = FolderMemoryStore(tmp_path / "memories")

    with pytest.raises(ValueError, match="channel_compacted_cap_num must be >= 0"):
        select_memory_ids_by_in_channel(
            store,
            in_channel="test",
            compacted_cap_num=-1,
        )
    with pytest.raises(ValueError, match="channel_raw_pair_cap_num must be >= 0"):
        select_memory_ids_by_in_channel(
            store,
            in_channel="test",
            raw_pair_cap_num=-1,
        )
    with pytest.raises(ValueError, match="contact_compacted_cap_num must be >= 0"):
        select_memory_ids_by_contact(
            store,
            contacts=[],
            compacted_cap_num=-1,
        )
    with pytest.raises(ValueError, match="contact_raw_pair_cap_num must be >= 0"):
        select_memory_ids_by_contact(
            store,
            contacts=[],
            raw_pair_cap_num=-1,
        )

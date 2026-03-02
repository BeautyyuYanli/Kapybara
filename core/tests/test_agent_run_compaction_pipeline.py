from __future__ import annotations

import importlib
from dataclasses import dataclass
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
            contact="test/system",
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
    assert '"contact":"test/system"' in event_meta
    assert '"content"' not in event_meta

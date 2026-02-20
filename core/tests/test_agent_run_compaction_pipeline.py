from __future__ import annotations

import importlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from k.agent.core.agent import agent, agent_run
from k.agent.core.entities import Event, MemoryHint
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config


@dataclass(slots=True)
class _FakeRunResult:
    output: MemoryHint
    _messages: list[ModelRequest | ModelResponse]

    def new_messages(self) -> list[ModelRequest | ModelResponse]:
        return list(self._messages)


@pytest.mark.anyio
async def test_agent_run_returns_compacted_memory_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured_user_prompt: tuple[Any, ...] | None = None

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
            output=MemoryHint(
                referenced_memory_ids=[],
                from_where_and_response_to_where="test",
                user_intents="test",
            ),
            _messages=messages,
        )

    async def fake_run_compaction(**_: Any) -> list[str]:
        return ["compacted-step"]

    monkeypatch.setattr(agent, "run", fake_agent_run)
    agent_module = importlib.import_module("k.agent.core.agent")
    monkeypatch.setattr(agent_module, "run_compaction", fake_run_compaction)

    config = Config(fs_base=tmp_path)
    memory_store = FolderMemoryStore(tmp_path / "memories")

    output_json, mem = await agent_run(
        model="test-model",
        config=config,
        memory_store=memory_store,
        instruct=Event(in_channel="test", content="do something"),
        parent_memories=[],
    )

    assert mem.compacted == ["compacted-step"]
    payload = json.loads(output_json)
    assert payload["from_where_and_response_to_where"] == "test"
    assert payload["user_intents"] == "test"

    assert captured_user_prompt is not None
    assert captured_user_prompt[3] == "do something"
    event_meta = captured_user_prompt[2]
    assert isinstance(event_meta, str)
    assert event_meta.startswith("<EventMeta>")
    assert '"in_channel":"test"' in event_meta
    assert '"content"' not in event_meta

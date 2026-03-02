from datetime import UTC
from pathlib import Path
from typing import Any

import anyio
import pytest
from kapy_collections.starters.telegram import runner as tg_runner
from kapy_collections.starters.telegram_mq import runner as tg_mq_runner

from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore
from k.agent.memory.paths import memory_root_from_config_base
from k.config import Config


def _sample_batch() -> list[dict[str, Any]]:
    return [
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "from": {"id": 42, "first_name": "Alice"},
                "chat": {"id": 99, "type": "private"},
                "date": 1_700_000_000,
                "text": "hello",
            },
        }
    ]


def _memory_store_from_config(config: Config) -> FolderMemoryStore:
    return FolderMemoryStore(root=memory_root_from_config_base(config.config_base))


@pytest.mark.anyio
async def test_telegram_runner_passes_parent_memories_none_to_agent_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def fake_agent_run(*args: Any, **kwargs: Any) -> MemoryRecord:
        _ = args
        captured_kwargs.update(kwargs)
        return MemoryRecord(
            in_channel="telegram/chat/99",
            contacts=["telegram/42"],
            input="hello",
            compacted=["step"],
            output="",
        )

    monkeypatch.setattr(tg_runner, "agent_run", fake_agent_run)

    config = Config(config_base=tmp_path / ".kapybara")
    await tg_runner.run_agent_for_chat_batch(
        api=object(),  # only used on error path
        chat_id=99,
        batch_updates=_sample_batch(),
        model=object(),
        config=config,
        memory_store=_memory_store_from_config(config),
        append_lock=anyio.Lock(),
        tz=UTC,
    )

    assert "parent_memories" in captured_kwargs
    assert captured_kwargs["parent_memories"] is None


@pytest.mark.anyio
async def test_telegram_mq_runner_passes_parent_memories_none_to_agent_run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    captured_kwargs: dict[str, Any] = {}

    async def fake_agent_run(*args: Any, **kwargs: Any) -> MemoryRecord:
        _ = args
        captured_kwargs.update(kwargs)
        return MemoryRecord(
            in_channel="telegram/chat/99",
            contacts=["telegram/42"],
            input="hello",
            compacted=["step"],
            output="",
        )

    monkeypatch.setattr(tg_mq_runner, "agent_run", fake_agent_run)

    config = Config(config_base=tmp_path / ".kapybara")
    await tg_mq_runner.run_agent_for_chat_batch(
        api=None,
        chat_id=99,
        batch_updates=_sample_batch(),
        model=object(),
        config=config,
        memory_store=_memory_store_from_config(config),
        append_lock=anyio.Lock(),
        tz=UTC,
    )

    assert "parent_memories" in captured_kwargs
    assert captured_kwargs["parent_memories"] is None

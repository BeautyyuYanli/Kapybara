from datetime import datetime
from pathlib import Path

import pytest

from k.agent.core.agent import MyDeps
from k.agent.core.entities import Event
from k.agent.core.memory_injection import MemoryRunState
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config


@pytest.mark.anyio
async def test_mydeps_async_context_closes_cleanly(tmp_path: Path) -> None:
    config = Config(config_base=tmp_path / ".kapybara")
    memory_store = FolderMemoryStore(config.config_base / "memories")

    deps = MyDeps(
        config=config,
        memory_storage=memory_store,
        memory_run=MemoryRunState(
            working_memory_created_at=datetime.now(),
            resolved_contact_ids=["c1"],
            injected_memories_prompt="",
            memory_parents=[],
            explicit_parent_memory_ids=[],
        ),
        start_event=Event(
            in_channel="test",
            contacts=["test/system"],
            content="healthcheck",
        ),
    )

    async with deps:
        assert deps._closed is False

    assert deps._closed is True

    # Ensure `close()` is idempotent.
    await deps.close()

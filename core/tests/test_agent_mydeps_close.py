from pathlib import Path

import pytest

from k.agent.core.agent import MyDeps
from k.agent.core.entities import Event
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config


@pytest.mark.anyio
async def test_mydeps_async_context_closes_cleanly(tmp_path: Path) -> None:
    config = Config(config_base=tmp_path / ".kapybara")
    memory_store = FolderMemoryStore(config.config_base / "memories")

    deps = MyDeps(
        config=config,
        memory_storage=memory_store,
        memory_parents=[],
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

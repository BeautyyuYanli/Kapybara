from pathlib import Path

import pytest

from k.agent.core.agent import MyDeps
from k.agent.memory.folder import FolderMemoryStore
from k.config import Config


@pytest.mark.anyio
async def test_mydeps_async_context_closes_cleanly(tmp_path: Path) -> None:
    config = Config(fs_base=tmp_path)
    memory_store = FolderMemoryStore(tmp_path / "memories")

    deps = MyDeps(
        config=config,
        memory_storage=memory_store,
        memory_parents=[],
        input_event_in_channel="test",
    )

    async with deps:
        assert deps._closed is False

    assert deps._closed is True

    # Ensure `close()` is idempotent.
    await deps.close()

from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic_ai.models.openai import OpenAIChatModel

import k.agent.core.run as run_module
from k.agent.core.entities import Event
from k.agent.memory.entities import MemoryRecord
from k.agent.memory.folder import FolderMemoryStore
from k.agent.memory.paths import memory_root_from_config_base
from k.config import Config, config_toml_path


def _write_cli_config(config_base: Path) -> None:
    path = config_toml_path(config_base)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[agent_run]\nmodel_name = "gpt-5.2"\n', encoding="utf-8")


@pytest.mark.anyio
async def test_run_once_uses_model_name_from_config_toml(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config_base = tmp_path / ".kapybara"
    _write_cli_config(config_base)

    async def fake_agent_run(
        *,
        model: Any,
        config: Config,
        memory_store: FolderMemoryStore,
        instruct: Event,
        message_history: Any = None,
        parent_memories: list[str] | None = None,
    ) -> MemoryRecord:
        _ = config, memory_store, message_history, parent_memories
        captured["model"] = model
        captured["instruct"] = instruct
        return MemoryRecord(
            in_channel=instruct.in_channel,
            out_channel=instruct.out_channel,
            input=instruct.content,
            compacted=["ok"],
        )

    monkeypatch.setattr(run_module, "agent_run", fake_agent_run)

    config = Config(config_base=config_base)
    memory_store = FolderMemoryStore(
        root=memory_root_from_config_base(config.config_base),
    )

    result = await run_module.run_once(
        config=config,
        memory_store=memory_store,
        prompt="hello from cli",
    )

    model = captured["model"]
    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "gpt-5.2"

    instruct = captured["instruct"]
    assert instruct == Event(
        in_channel="direct_input",
        contacts=[],
        content="hello from cli",
    )
    assert '"compacted":["ok"]' in result


@pytest.mark.anyio
async def test_run_once_waits_for_parent_memories_before_calling_agent_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config_base = tmp_path / ".kapybara"
    _write_cli_config(config_base)

    async def fake_agent_run(
        *,
        model: Any,
        config: Config,
        memory_store: FolderMemoryStore,
        instruct: Event,
        message_history: Any = None,
        parent_memories: list[str] | None = None,
    ) -> MemoryRecord:
        _ = model, config, memory_store, instruct, message_history
        captured["parent_memories"] = parent_memories
        return MemoryRecord(
            in_channel="direct_input",
            input="hello from cli",
            compacted=["ok"],
        )

    monkeypatch.setattr(run_module, "agent_run", fake_agent_run)

    config = Config(config_base=config_base)
    memory_store = FolderMemoryStore(
        root=memory_root_from_config_base(config.config_base),
    )
    parent = MemoryRecord(
        in_channel="dependency",
        input="parent",
        output="",
    )

    async def publish_parent() -> None:
        await anyio.sleep(0.05)
        memory_store.append(parent)

    async with anyio.create_task_group() as task_group:
        task_group.start_soon(publish_parent)
        await run_module.run_once(
            config=config,
            memory_store=memory_store,
            prompt="hello from cli",
            parent_memories=[parent.id_],
            parents_timeout_seconds=1,
        )

    assert captured["parent_memories"] == [parent.id_]


@pytest.mark.anyio
async def test_run_once_preserves_explicit_empty_contacts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config_base = tmp_path / ".kapybara"
    _write_cli_config(config_base)

    async def fake_agent_run(
        *,
        model: Any,
        config: Config,
        memory_store: FolderMemoryStore,
        instruct: Event,
        message_history: Any = None,
        parent_memories: list[str] | None = None,
    ) -> MemoryRecord:
        _ = model, config, memory_store, message_history, parent_memories
        captured["instruct"] = instruct
        return MemoryRecord(
            in_channel=instruct.in_channel,
            out_channel=instruct.out_channel,
            input=instruct.content,
            compacted=["ok"],
        )

    monkeypatch.setattr(run_module, "agent_run", fake_agent_run)

    config = Config(config_base=config_base)
    memory_store = FolderMemoryStore(
        root=memory_root_from_config_base(config.config_base),
    )

    await run_module.run_once(
        config=config,
        memory_store=memory_store,
        prompt="hello from cli",
        in_channel="self-hook/test",
        contacts=[],
    )

    assert captured["instruct"] == Event(
        in_channel="self-hook/test",
        contacts=[],
        content="hello from cli",
    )


@pytest.mark.anyio
async def test_run_once_times_out_when_parent_memories_never_appear(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    called = False
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    config_base = tmp_path / ".kapybara"
    _write_cli_config(config_base)

    async def fake_agent_run(
        *,
        model: Any,
        config: Config,
        memory_store: FolderMemoryStore,
        instruct: Event,
        message_history: Any = None,
        parent_memories: list[str] | None = None,
    ) -> MemoryRecord:
        nonlocal called
        _ = model, config, memory_store, instruct, message_history, parent_memories
        called = True
        return MemoryRecord(
            in_channel="direct_input",
            input="hello from cli",
            compacted=["ok"],
        )

    monkeypatch.setattr(run_module, "agent_run", fake_agent_run)

    config = Config(config_base=config_base)
    memory_store = FolderMemoryStore(
        root=memory_root_from_config_base(config.config_base),
    )
    missing_parent_id = MemoryRecord(
        in_channel="dependency",
        input="missing parent",
        output="",
    ).id_

    with pytest.raises(TimeoutError, match=missing_parent_id):
        await run_module.run_once(
            config=config,
            memory_store=memory_store,
            prompt="hello from cli",
            parent_memories=[missing_parent_id],
            parents_timeout_seconds=0.05,
        )

    assert called is False


@pytest.mark.anyio
async def test_main_omits_contacts_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}

    async def fake_run_once(
        *,
        config: Config,
        memory_store: FolderMemoryStore,
        prompt: str,
        in_channel: str = "direct_input",
        contacts: list[str] | None = None,
        out_channel: str | None = None,
        parent_memories: list[str] | None = None,
        parents_timeout_seconds: float = 300.0,
        model: Any = None,
    ) -> str:
        _ = config, memory_store, prompt, out_channel, parent_memories
        _ = parents_timeout_seconds, model
        captured["in_channel"] = in_channel
        captured["contacts"] = contacts
        return '{"compacted":["ok"]}'

    monkeypatch.setattr(run_module, "run_once", fake_run_once)
    monkeypatch.setattr(run_module, "_agent_run_model_from_config", lambda config: "m")

    config_base = tmp_path / ".kapybara"
    await run_module.main(
        [
            "--config-base",
            str(config_base),
            "--in-channel",
            "self-hook/test",
            "hello from cli",
        ]
    )

    assert captured == {
        "in_channel": "self-hook/test",
        "contacts": [],
    }

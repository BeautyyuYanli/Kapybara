import logging
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from pydantic_ai.models.openai import OpenAIChatModel

import k.agent.core.run as run_module
from k.agent.core.entities import Event
from k.agent.memory.entities import MemoryRecord, memory_record_id_from_millis
from k.agent.memory.folder import FolderMemoryStore
from k.agent.memory.paths import memory_root_from_config_base
from k.config import Config, config_toml_path


class _CapturedLogger:
    """Minimal logger double for asserting visible wait failures."""

    def __init__(self) -> None:
        self.error_messages: list[str] = []
        self.exception_messages: list[str] = []

    def error(self, msg: str, *args: object) -> None:
        self.error_messages.append(msg % args if args else msg)

    def exception(self, msg: str, *args: object) -> None:
        self.exception_messages.append(msg % args if args else msg)


def _write_cli_config(
    config_base: Path,
    *,
    openai_api_key: str | None = None,
    openai_base_url: str | None = None,
) -> None:
    path = config_toml_path(config_base)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["[agent_run]", 'model_name = "gpt-5.2"']
    if openai_api_key is not None:
        lines.append(f'openai_api_key = "{openai_api_key}"')
    if openai_base_url is not None:
        lines.append(f'openai_base_url = "{openai_base_url}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_configure_cli_logfire_uses_token_optional_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    handler = object()

    def fake_configure(**kwargs: Any) -> None:
        captured["configure"] = kwargs

    def fake_instrument_pydantic_ai() -> None:
        captured["instrumented"] = True

    def fake_basic_config(**kwargs: Any) -> None:
        captured["basic_config"] = kwargs

    monkeypatch.setattr(run_module.logfire, "configure", fake_configure)
    monkeypatch.setattr(
        run_module.logfire,
        "instrument_pydantic_ai",
        fake_instrument_pydantic_ai,
    )
    monkeypatch.setattr(run_module.logfire, "LogfireLoggingHandler", lambda: handler)
    monkeypatch.setattr(run_module.logging, "basicConfig", fake_basic_config)

    run_module._configure_cli_logfire()

    assert captured["configure"] == {"send_to_logfire": "if-token-present"}
    assert captured["instrumented"] is True
    assert captured["basic_config"] == {
        "level": logging.INFO,
        "handlers": [handler],
    }


def test_child_cli_argv_omits_async_flag() -> None:
    assert run_module._child_cli_argv(
        [
            "--async",
            "--out-channel",
            "self-hook/test",
            "hello from cli",
        ]
    ) == [
        "--out-channel",
        "self-hook/test",
        "hello from cli",
    ]


def test_parse_cli_args_accepts_parent_memory_ids_that_start_with_dash() -> None:
    dashed_parent_id = memory_record_id_from_millis(0)

    args = run_module._parse_cli_args(
        [
            "--parent-memory",
            dashed_parent_id,
            "--out-channel",
            "self-hook/reply",
            "hello from cli",
        ]
    )

    assert args.parent_memories == [dashed_parent_id]
    assert args.out_channel == "self-hook/reply"
    assert args.prompt == "hello from cli"


def test_parse_cli_args_keeps_missing_parent_memory_errors_for_real_options() -> None:
    with pytest.raises(SystemExit):
        run_module._parse_cli_args(
            [
                "--parent-memory",
                "--out-channel",
                "self-hook/reply",
                "hello from cli",
            ]
        )


def test_agent_run_model_from_config_uses_optional_openai_overrides(
    tmp_path: Path,
) -> None:
    config_base = tmp_path / ".kapybara"
    _write_cli_config(
        config_base,
        openai_api_key="toml-key",
        openai_base_url="https://gateway.example/v1",
    )

    model = run_module._agent_run_model_from_config(Config(config_base=config_base))

    assert isinstance(model, OpenAIChatModel)
    assert model.model_name == "gpt-5.2"
    assert str(model.client.base_url) == "https://gateway.example/v1/"
    assert model.client.api_key == "toml-key"


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
    monkeypatch.setattr(
        run_module.uuid,
        "uuid4",
        lambda: type("FakeUuid", (), {"hex": "oneshot"})(),
    )

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
        in_channel="direct/oneshot",
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
            in_channel=instruct.in_channel,
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
    logger = _CapturedLogger()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(run_module, "logger", logger)
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
            in_channel=instruct.in_channel,
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
    assert logger.error_messages == [
        f"Timed out waiting for parent memories: {missing_parent_id}"
    ]
    assert logger.exception_messages == []


@pytest.mark.anyio
async def test_wait_for_parent_memories_prefers_contains_id_when_available() -> None:
    parent_id = MemoryRecord(
        in_channel="dependency",
        input="parent",
        output="",
    ).id_

    class CheapMemoryStore:
        def __init__(self) -> None:
            self.contains_calls = 0
            self.get_by_id_calls = 0

        def refresh(self) -> None:
            return None

        def contains_id(self, id_: str) -> bool:
            assert id_ == parent_id
            self.contains_calls += 1
            return self.contains_calls >= 2

        def get_by_id(self, id_: str) -> MemoryRecord | None:
            _ = id_
            self.get_by_id_calls += 1
            raise AssertionError("wait path should prefer contains_id()")

    store = CheapMemoryStore()

    await run_module._wait_for_parent_memories(
        memory_store=store,
        parent_memories=[parent_id],
        timeout_seconds=1,
        poll_interval_seconds=0.01,
    )

    assert store.contains_calls >= 2
    assert store.get_by_id_calls == 0


@pytest.mark.anyio
async def test_wait_for_parent_memories_logs_store_errors_before_reraising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = _CapturedLogger()
    monkeypatch.setattr(run_module, "logger", logger)

    parent_id = MemoryRecord(
        in_channel="dependency",
        input="parent",
        output="",
    ).id_

    class BrokenMemoryStore:
        def refresh(self) -> None:
            raise RuntimeError("disk unavailable")

        def contains_id(self, id_: str) -> bool:
            _ = id_
            raise RuntimeError("disk unavailable")

        def get_by_id(self, id_: str) -> MemoryRecord | None:
            _ = id_
            return None

    with pytest.raises(RuntimeError, match="disk unavailable"):
        await run_module._wait_for_parent_memories(
            memory_store=BrokenMemoryStore(),
            parent_memories=[parent_id],
            timeout_seconds=1,
        )

    assert logger.error_messages == []
    assert logger.exception_messages == [
        f"Failed while waiting for parent memories: {parent_id}"
    ]


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
        in_channel: str | None = None,
        contacts: list[str] | None = None,
        out_channel: str | None = None,
        parent_memories: list[str] | None = None,
        parents_timeout_seconds: float = 300.0,
        model: Any = None,
    ) -> str:
        _ = memory_store, prompt, out_channel, parent_memories
        _ = parents_timeout_seconds, model
        captured["config_base"] = config.config_base
        captured["in_channel"] = in_channel
        captured["contacts"] = contacts
        return '{"compacted":["ok"]}'

    monkeypatch.setattr(run_module, "run_once", fake_run_once)
    monkeypatch.setattr(run_module, "_agent_run_model_from_config", lambda config: "m")

    config_base = tmp_path / ".kapybara"
    monkeypatch.setenv("HOME", str(tmp_path))
    await run_module.main(
        [
            "--in-channel",
            "self-hook/test",
            "hello from cli",
        ]
    )

    assert captured == {
        "config_base": config_base.resolve(),
        "in_channel": "self-hook/test",
        "contacts": [],
    }


@pytest.mark.anyio
async def test_main_async_mode_spawns_detached_child_and_prints_pid_and_logfile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(run_module, "_configure_cli_logfire", lambda: None)
    monkeypatch.setattr(
        "rich.print", lambda message: captured.setdefault("printed", message)
    )

    def fake_popen(
        cmd: list[str],
        *,
        stdin: object,
        stdout: Any,
        stderr: object,
        start_new_session: bool,
        env: dict[str, str],
    ) -> Any:
        captured["cmd"] = cmd
        captured["stdin"] = stdin
        captured["stdout_name"] = stdout.name
        captured["stderr"] = stderr
        captured["start_new_session"] = start_new_session
        captured["env"] = env
        return type("FakeProcess", (), {"pid": 43210})()

    monkeypatch.setattr(run_module.subprocess, "Popen", fake_popen)

    await run_module.main(
        [
            "--async",
            "--out-channel",
            "self-hook/reply",
            "hello from cli",
        ]
    )

    config_base = (tmp_path / ".kapybara").resolve()
    log_path = Path(captured["stdout_name"])
    assert captured["cmd"] == [
        sys.executable,
        "-m",
        "k.agent.core.run",
        "--out-channel",
        "self-hook/reply",
        "hello from cli",
    ]
    assert captured["stdin"] is run_module.subprocess.DEVNULL
    assert captured["stderr"] is run_module.subprocess.STDOUT
    assert captured["start_new_session"] is True
    assert captured["env"]["K_CONFIG_BASE"] == str(config_base)
    assert log_path.exists()
    assert log_path.parent == config_base / "logs" / "kapy"
    assert captured["printed"] == f"pid=43210 logfile={log_path}"


@pytest.mark.anyio
async def test_main_async_mode_requires_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_module, "_configure_cli_logfire", lambda: None)

    with pytest.raises(SystemExit, match="--async requires a prompt"):
        await run_module.main(["--async"])


@pytest.mark.anyio
async def test_run_repl_uses_direct_default_when_in_channel_omitted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    prompts = iter(["hello from repl", "quit"])

    async def fake_run_once(
        *,
        config: Config,
        memory_store: FolderMemoryStore,
        prompt: str,
        in_channel: str | None = None,
        contacts: list[str] | None = None,
        out_channel: str | None = None,
        parent_memories: list[str] | None = None,
        parents_timeout_seconds: float = 300.0,
        model: Any = None,
    ) -> str:
        _ = config, memory_store, out_channel, parent_memories
        _ = parents_timeout_seconds, model
        captured["prompt"] = prompt
        captured["in_channel"] = in_channel
        captured["contacts"] = contacts
        return '{"compacted":["ok"]}'

    monkeypatch.setattr(run_module, "run_once", fake_run_once)
    monkeypatch.setattr("builtins.input", lambda _: next(prompts))

    config = Config(config_base=tmp_path / ".kapybara")
    memory_store = FolderMemoryStore(
        root=memory_root_from_config_base(config.config_base),
    )

    await run_module.run_repl(
        config=config,
        memory_store=memory_store,
        model="m",
    )

    assert captured == {
        "prompt": "hello from repl",
        "in_channel": "direct/default",
        "contacts": [],
    }

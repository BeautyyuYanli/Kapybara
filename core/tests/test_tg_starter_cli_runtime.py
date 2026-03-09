from pathlib import Path
from typing import Any

import pytest
from kapy_collections.starters.telegram import cli as tg_cli
from kapy_collections.starters.telegram_mq import runner as tg_mq_runner

from k.config import Config


@pytest.mark.anyio
async def test_telegram_cli_run_uses_shared_kapy_runtime_helpers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    config = Config(config_base=tmp_path / ".kapybara")

    def fake_config() -> Config:
        return config

    def fake_configure_cli_logfire(config_base: Path) -> None:
        captured["logfire_config_base"] = config_base

    def fake_resolve_cli_model(cfg: Config, model: object) -> object:
        captured["resolved_model_args"] = (cfg, model)
        return "resolved-model"

    async def fake_poll_and_run_forever(**kwargs: Any) -> None:
        captured["runner_kwargs"] = kwargs

    monkeypatch.setattr(tg_cli, "Config", fake_config)
    monkeypatch.setattr(tg_cli, "configure_cli_logfire", fake_configure_cli_logfire)
    monkeypatch.setattr(tg_cli, "resolve_cli_model", fake_resolve_cli_model)
    monkeypatch.setattr(tg_cli, "_poll_and_run_forever", fake_poll_and_run_forever)

    await tg_cli.run(
        token="bot-token",
        keyword="kapy",
        model=None,
        chat_id="123",
    )

    assert captured["logfire_config_base"] == config.config_base
    assert captured["resolved_model_args"] == (config, None)
    assert captured["runner_kwargs"]["config"] == config
    assert captured["runner_kwargs"]["model"] == "resolved-model"


@pytest.mark.anyio
async def test_telegram_mq_run_uses_shared_kapy_runtime_helpers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, Any] = {}
    config = Config(config_base=tmp_path / ".kapybara")

    def fake_config() -> Config:
        return config

    def fake_configure_cli_logfire(config_base: Path) -> None:
        captured["logfire_config_base"] = config_base

    def fake_resolve_cli_model(cfg: Config, model: object) -> object:
        captured["resolved_model_args"] = (cfg, model)
        return "resolved-model"

    async def fake_run_amqp_forever(**kwargs: Any) -> None:
        captured["runner_kwargs"] = kwargs

    monkeypatch.setattr(tg_mq_runner, "Config", fake_config)
    monkeypatch.setattr(
        tg_mq_runner,
        "configure_cli_logfire",
        fake_configure_cli_logfire,
    )
    monkeypatch.setattr(tg_mq_runner, "resolve_cli_model", fake_resolve_cli_model)
    monkeypatch.setattr(tg_mq_runner, "run_amqp_forever", fake_run_amqp_forever)

    await tg_mq_runner.run(
        amqp_url="amqp://guest:guest@localhost/",
        keyword="kapy",
        model=None,
        chat_id="123",
    )

    assert captured["logfire_config_base"] == config.config_base
    assert captured["resolved_model_args"] == (config, None)
    assert captured["runner_kwargs"]["config"] == config
    assert captured["runner_kwargs"]["model"] == "resolved-model"

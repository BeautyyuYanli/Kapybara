"""Shared runtime helpers for installed CLIs and long-running starters.

These helpers centralize two behaviors that should stay aligned across entry
points that eventually call `k.agent.core.agent_run`:

- Logfire setup that never prompts for project credentials in ad-hoc shells.
- Model resolution from `<config_base>/config.toml` using the same optional
  OpenAI provider overrides as the installed `kapy` CLI.

Callers may still pass an explicit `Model` instance when they need custom
runtime behavior (for example a fallback model graph). String model ids are
treated as config-backed overrides and still inherit the TOML OpenAI settings.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

import logfire

from k.config import Config, load_kapybara_toml_config

if TYPE_CHECKING:
    from pydantic_ai.models import Model


def load_cli_logfire_token(config_base: str | Path) -> str | None:
    """Best-effort lookup of `[logfire].token` from `<config_base>/config.toml`.

    Logfire setup should not fail earlier than the main model-config path. When
    the TOML file is missing or invalid, keep the environment-driven behavior
    here and let the later model-config load surface the real error.
    """

    try:
        file_config = load_kapybara_toml_config(config_base)
    except ValueError:
        return None

    if file_config.logfire is None:
        return None
    return file_config.logfire.token


def configure_cli_logfire(config_base: str | Path) -> None:
    """Configure Logfire for CLI-like runtimes without interactive setup.

    The default `logfire.configure()` behavior prompts for project setup when
    no token or cached credentials are present. Long-running starters and the
    installed `kapy` CLI both need non-interactive behavior, so only send
    telemetry when Logfire is already configured and always disable console
    output. If `<config_base>/config.toml` declares `[logfire].token`, prefer
    that token for all of these runtimes.
    """

    token = load_cli_logfire_token(config_base)
    if token is None:
        logfire.configure(send_to_logfire="if-token-present", console=False)
    else:
        logfire.configure(
            send_to_logfire="if-token-present",
            token=token,
            console=False,
        )
    logfire.instrument_pydantic_ai()
    logging.basicConfig(level=logging.INFO, handlers=[logfire.LogfireLoggingHandler()])


def agent_run_model_from_config(
    config: Config,
    *,
    model_name: str | None = None,
) -> Model:
    """Build the default `agent_run` model from `<config_base>/config.toml`.

    `kapy` uses `OpenAIChatModel`, so the resolved model name must be an OpenAI
    model id such as `gpt-5.2`. Optional TOML `openai_api_key` and
    `openai_base_url` values override the default OpenAI provider resolution
    for CLI-like runs; when omitted, the provider still falls back to the
    environment/default client behavior.

    When `model_name` is provided, it overrides only the model id while keeping
    the same provider settings that `kapy` would load from the TOML file.
    """

    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    file_config = load_kapybara_toml_config(config.config_base)
    resolved_model_name = (
        file_config.agent_run.model_name if model_name is None else model_name.strip()
    )
    if not resolved_model_name:
        raise ValueError("model_name must not be empty")

    return OpenAIChatModel(
        resolved_model_name,
        provider=OpenAIProvider(
            api_key=file_config.agent_run.openai_api_key,
            base_url=file_config.agent_run.openai_base_url,
        ),
    )


def resolve_cli_model(
    config: Config,
    model: Model | str | None,
) -> Model:
    """Resolve a CLI/starter model input to a concrete `Model` instance.

    `None` means "use the same config-backed default as `kapy`". A string is
    treated as a model-id override on top of that same config source. Existing
    `Model` instances are passed through unchanged so programmatic callers can
    still supply custom model graphs.
    """

    if model is None:
        return agent_run_model_from_config(config)
    if isinstance(model, str):
        return agent_run_model_from_config(config, model_name=model)
    return model

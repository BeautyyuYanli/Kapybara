"""Shared runtime helpers for installed CLIs and long-running starters.

These helpers centralize two behaviors that should stay aligned across entry
points that eventually call `k.agent.core.agent_run`:

- Logfire setup that never prompts for project credentials in ad-hoc shells.
- Model resolution from `<config_base>/config.toml` using the same optional
  provider overrides as the installed `kapy` CLI.

Callers may still pass an explicit `Model` instance when they need custom
runtime behavior (for example a fallback model graph). String model ids are
treated as config-backed overrides and still inherit the TOML provider
settings selected by `[agent_run].provider`.
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

    `[agent_run].provider` selects the `pydantic-ai` model/provider family.
    Supported providers are `openai`, `google`, and `anthropic` (with the TOML
    aliases `gemini` -> `google` and `claude` -> `anthropic`). Provider-specific
    `*_api_key` and `*_base_url` values override the default SDK/client
    resolution for CLI-like runs; when omitted, each provider still falls back
    to its environment/default client behavior.

    When `model_name` is provided, it overrides only the model id while keeping
    the same provider settings that `kapy` would load from the TOML file.
    """

    file_config = load_kapybara_toml_config(config.config_base)
    agent_run_config = file_config.agent_run
    resolved_model_name = (
        agent_run_config.model_name if model_name is None else model_name.strip()
    )
    if not resolved_model_name:
        raise ValueError("model_name must not be empty")

    if agent_run_config.provider == "openai":
        from pydantic_ai.models.openai import OpenAIChatModel
        from pydantic_ai.providers.openai import OpenAIProvider

        return OpenAIChatModel(
            resolved_model_name,
            provider=OpenAIProvider(
                api_key=agent_run_config.openai_api_key,
                base_url=agent_run_config.openai_base_url,
            ),
        )
    if agent_run_config.provider == "google":
        from pydantic_ai.models.google import GoogleModel
        from pydantic_ai.providers.google import GoogleProvider

        return GoogleModel(
            resolved_model_name,
            provider=GoogleProvider(
                api_key=agent_run_config.google_api_key,
                base_url=agent_run_config.google_base_url,
            ),
        )
    if agent_run_config.provider == "anthropic":
        from anthropic import AsyncAnthropic
        from pydantic_ai.models.anthropic import AnthropicModel
        from pydantic_ai.providers.anthropic import AnthropicProvider

        client = AsyncAnthropic(
            api_key=agent_run_config.anthropic_api_key,
            base_url=agent_run_config.anthropic_base_url,
            default_headers=agent_run_config.anthropic_default_headers or {},
        )
        return AnthropicModel(
            resolved_model_name,
            provider=AnthropicProvider(anthropic_client=client),
        )

    raise ValueError(f"Unsupported agent_run provider: {agent_run_config.provider!r}")


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

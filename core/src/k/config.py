"""Application runtime configuration.

`config_base` is the canonical filesystem root for Kapybara state. It points
to the `.kapybara` directory itself (for example: `./data/fs/.kapybara`).

This module has two config layers:
- `Config`: constructor/env-driven transport settings for the Python process.
- `KapybaraTomlConfig`: user-editable defaults loaded from
  `<config_base>/config.toml` for installed CLIs such as `kapy`, including
  model defaults plus optional ancillary integrations like Logfire.
"""

import tomllib
from pathlib import Path

from pydantic import (
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

_CONFIG_TOML_FILENAME = "config.toml"


def _normalize_optional_toml_string(value: str | None) -> str | None:
    """Trim optional TOML strings and collapse blank values to `None`."""

    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


class AgentRunCliConfig(BaseModel):
    """TOML-backed defaults for the installed `kapy` console script.

    `provider` selects which `pydantic-ai` model family to build for
    `k.agent.core.agent.agent_run`. The default is `openai` for backward
    compatibility; `gemini` and `claude` are accepted as aliases for the
    underlying `google` and `anthropic` providers.

    `model_name` is the provider-specific model id. Provider-specific `*_api_key`
    and `*_base_url` fields override the default SDK/client resolution for that
    provider only. When omitted, provider resolution falls back to the
    environment/default behavior of the underlying `pydantic-ai` provider.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str = "openai"
    model_name: str
    openai_api_key: str | None = None
    openai_base_url: str | None = None
    google_api_key: str | None = None
    google_base_url: str | None = None
    anthropic_api_key: str | None = None
    anthropic_base_url: str | None = None

    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        """Normalize provider aliases and reject unsupported values."""

        normalized = value.strip().lower()
        if normalized == "gemini":
            normalized = "google"
        elif normalized == "claude":
            normalized = "anthropic"

        if normalized not in {"openai", "google", "anthropic"}:
            raise ValueError(
                "provider must be one of: openai, google, anthropic "
                "(aliases: gemini -> google, claude -> anthropic)"
            )
        return normalized

    @field_validator("model_name")
    @classmethod
    def _validate_model_name(cls, value: str) -> str:
        """Require a non-empty model id after trimming user whitespace."""

        normalized = value.strip()
        if not normalized:
            raise ValueError("model_name must not be empty")
        return normalized

    @field_validator(
        "openai_api_key",
        "openai_base_url",
        "google_api_key",
        "google_base_url",
        "anthropic_api_key",
        "anthropic_base_url",
    )
    @classmethod
    def _normalize_optional_provider_setting(cls, value: str | None) -> str | None:
        """Trim optional provider settings and collapse blank strings to `None`."""

        return _normalize_optional_toml_string(value)


class LogfireCliConfig(BaseModel):
    """TOML-backed Logfire defaults for installed CLIs.

    `token`, when set, is passed to `logfire.configure()` for `kapy` runs.
    Omitting the section or leaving the value blank preserves the existing
    environment/cached-credentials resolution path.
    """

    model_config = ConfigDict(extra="forbid")

    token: str | None = None

    @field_validator("token")
    @classmethod
    def _normalize_optional_token(cls, value: str | None) -> str | None:
        """Trim optional Logfire tokens and collapse blank strings to `None`."""

        return _normalize_optional_toml_string(value)


class KapybaraTomlConfig(BaseModel):
    """Contents of `<config_base>/config.toml`.

    This file stores persistent, user-editable defaults that should live with
    the agent's filesystem state. `[agent_run]` carries required `kapy`
    model/provider defaults, while optional sections such as `[logfire]`
    configure ancillary CLI integrations. Transport and path settings stay on
    `Config` so they can still be controlled through `K_*` environment
    variables.
    """

    model_config = ConfigDict(extra="forbid")

    agent_run: AgentRunCliConfig
    logfire: LogfireCliConfig | None = None


def config_toml_path(config_base: str | Path) -> Path:
    """Return the canonical `config.toml` path under `config_base`."""

    return Path(config_base).expanduser().resolve() / _CONFIG_TOML_FILENAME


def load_kapybara_toml_config(config_base: str | Path) -> KapybaraTomlConfig:
    """Load `<config_base>/config.toml`.

    Raises:
        ValueError: when the file is missing or cannot be read, parsed, or validated.
    """

    path = config_toml_path(config_base)
    if not path.exists():
        raise ValueError(f"Expected config file at {path}, but no such file exists")
    if not path.is_file():
        raise ValueError(f"Expected config file at {path}, found non-file entry")

    try:
        with path.open("rb") as f:
            payload = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"Failed to parse config file at {path}: {e}") from e
    except OSError as e:
        raise ValueError(f"Failed to read config file at {path}: {e}") from e

    try:
        return KapybaraTomlConfig.model_validate(payload)
    except ValidationError as e:
        raise ValueError(f"Invalid config file at {path}: {e}") from e


class Config(BaseSettings):
    """Settings loaded from constructor kwargs and `K_*` environment variables.

    Invariant:
        `config_base` always points at the `.kapybara` directory.
        `ssh_key` is normalized to an absolute path at init time and is
        not interpreted relative to `config_base`.
        `ssh_user` and `ssh_addr` are configured together for SSH mode, or both
        are `None` to enable local command execution mode.
    """

    model_config = SettingsConfigDict(env_prefix="K_")

    config_base: Path = Path("~/.kapybara")
    ssh_user: str | None = None
    ssh_addr: str | None = None
    ssh_port: int = 22
    ssh_key: Path = Path("~/.ssh/id_ed25519")

    @field_validator("config_base", "ssh_key")
    @classmethod
    def _normalize_path_settings(cls, value: Path) -> Path:
        """Normalize path-like settings to absolute paths."""

        return value.expanduser().resolve()

    @model_validator(mode="after")
    def _validate_ssh_target(self) -> "Config":
        """Require both SSH endpoint parts or neither.

        When both are `None`, command execution falls back to local mode.
        """

        if (self.ssh_user is None) ^ (self.ssh_addr is None):
            raise ValueError(
                "ssh_user and ssh_addr must either both be set or both be None"
            )
        return self

"""Explicit environment mapping shared by the control and execution CLIs."""

import re
from pathlib import Path
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KAPY_",
        env_file=None,
        extra="ignore",
        populate_by_name=True,
    )

    openai_base_url: str = Field("https://api.openai.com/v1", validation_alias="OPENAI_BASE_URL")
    openai_api_key: SecretStr | None = Field(None, validation_alias="OPENAI_API_KEY")
    openai_model: str = Field("gpt-5.6-luna", validation_alias="OPENAI_MODEL")
    telegram_bot_token: SecretStr | None = Field(None, validation_alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: int | None = Field(None, validation_alias="TELEGRAM_CHAT_ID")
    database_url: SecretStr = SecretStr("postgresql://kapy:kapy-local@127.0.0.1:55432/kapy")
    valkey_url: SecretStr = SecretStr("redis://127.0.0.1:56379/0")
    database_schema: str = "kapy_state"
    valkey_namespace: str = "kapy_state"
    control_url: str = "http://127.0.0.1:8000"
    control_token: SecretStr | None = None
    session_signing_key: SecretStr | None = None
    machine_tokens: dict[str, SecretStr] = Field(default_factory=dict)
    machine_id: str | None = None
    machine_token: SecretStr | None = None
    session_id: str | None = None
    session_token: SecretStr | None = None
    daemon_socket: Path | None = None
    execution_state_dir: Path | None = None
    execution_data_dir: Path | None = None
    execution_runtime_dir: Path | None = None
    child_env: dict[str, str] = Field(default_factory=dict)
    idle_disconnect_after_s: float | None = Field(None, gt=0)
    idle_reconnect_after_s: float = Field(30.0, gt=0)
    telegram_api_base: str = "https://api.telegram.org"
    context_window_tokens: int | None = Field(None, gt=0)
    max_output_tokens: int = Field(16_384, gt=0)
    compression_ratio: float = Field(0.70, gt=0, lt=1)
    keep_recent_ratio: float = Field(0.10, gt=0, lt=1)
    media_max_bytes: int = Field(20 * 1024 * 1024, gt=0)

    @field_validator(
        "openai_api_key",
        "telegram_bot_token",
        "telegram_chat_id",
        "control_token",
        "session_signing_key",
        "machine_token",
        "session_token",
        mode="before",
    )
    @classmethod
    def empty_optional(cls, value: object) -> object:
        return None if value == "" else value

    @field_validator("database_schema")
    @classmethod
    def schema_identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
            raise ValueError("database_schema must be a PostgreSQL identifier")
        return value

    @model_validator(mode="after")
    def validate_machines(self) -> Self:
        for machine_id, token in self.machine_tokens.items():
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", machine_id):
                raise ValueError("machine IDs must contain only letters, digits, _ and -")
            if not token.get_secret_value():
                raise ValueError("machine tokens must not be empty")
        return self

    def require_control(self) -> None:
        for name in ("control_token", "session_signing_key", "openai_api_key"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required for control-server")
        if self.context_window_tokens is None:
            raise ValueError("KAPY_CONTEXT_WINDOW_TOKENS is required for control-server")
        if self.telegram_bot_token and self.telegram_chat_id is None:
            raise ValueError("TELEGRAM_CHAT_ID is required when Telegram is enabled")


def load_settings(*, env_file: str | None = None) -> Settings:
    """Never discover .env; callers must explicitly opt into a particular file."""
    return Settings(_env_file=env_file)

"""Telegram-only configuration and one XDG path resolver shared by serve and db.

Configuration is read at command execution. Database commands instantiate only
StorageSettings and never validate bot, core database or model configuration.
"""

import os
from pathlib import Path

from platformdirs import user_state_path
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from kapy.tmpv2.application.settings import CommonSettings
from kapy.tmpv2.control.sessions import CreateSession


def database_path() -> Path:
    return user_state_path("kapy") / "plugins" / "telegram" / "telegram.sqlite3"


class StorageSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KAPY_TELEGRAM_", env_file=None, extra="ignore", populate_by_name=True
    )
    database_path: Path = Field(default_factory=database_path)

    @field_validator("database_path")
    @classmethod
    def absolute_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("database_path must be absolute")
        return value


class TelegramSettings(StorageSettings):
    common: CommonSettings = Field(
        default_factory=lambda: CommonSettings.model_validate(dict(os.environ))
    )
    bot_token: SecretStr = Field(validation_alias="TELEGRAM_BOT_TOKEN", min_length=1)
    allowed_chat_ids: set[int] = Field(min_length=1)
    session_template: CreateSession | None = None
    api_base: str = "https://api.telegram.org"
    poll_timeout: int = Field(default=25, ge=1, le=50)
    recovery_interval: float = Field(default=5, gt=0, allow_inf_nan=False)

"""HTTP command configuration; construction reads environment, import does not."""

import os
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from kapy.tmpv2.application.settings import CommonSettings


class HttpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="KAPY_HTTP_", env_file=None, extra="ignore", populate_by_name=True
    )
    common: CommonSettings = Field(
        default_factory=lambda: CommonSettings.model_validate(dict(os.environ))
    )
    host: str = "127.0.0.1"
    port: int = Field(default=8000, ge=1, le=65535)
    control_token: SecretStr = Field(validation_alias="KAPY_CONTROL_TOKEN", min_length=1)
    frontend_dist: Path | None = None
    shutdown_timeout: float = Field(default=15, gt=0, allow_inf_nan=False)

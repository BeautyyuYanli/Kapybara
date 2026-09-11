"""Shared value types only; each command explicitly supplies its environment values."""

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator


class CommonSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True, allow_inf_nan=False)

    database_url: SecretStr = Field(
        default=SecretStr("postgresql://kapy:kapy-local@127.0.0.1:55432/kapy"),
        validation_alias="KAPY_DATABASE_URL",
    )
    database_schema: str = Field(default="kapy_tmpv2", validation_alias="KAPY_DATABASE_SCHEMA")
    valkey_url: SecretStr = Field(
        default=SecretStr("valkey://127.0.0.1:56379/0"), validation_alias="KAPY_VALKEY_URL"
    )
    valkey_namespace: str = Field(default="kapy_tmpv2", validation_alias="KAPY_VALKEY_NAMESPACE")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", validation_alias="KAPY_LOG_LEVEL"
    )
    heartbeat_interval: float = Field(default=10, gt=0, validation_alias="KAPY_HEARTBEAT_INTERVAL")
    heartbeat_timeout: float = Field(default=60, gt=0, validation_alias="KAPY_HEARTBEAT_TIMEOUT")
    realtime_output: bool = Field(default=True, validation_alias="KAPY_REALTIME_OUTPUT")
    output_flush_interval: float = Field(
        default=0.5, ge=0, validation_alias="KAPY_OUTPUT_FLUSH_INTERVAL"
    )

    @field_validator("database_schema")
    @classmethod
    def schema_identifier(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
            raise ValueError("database_schema must be a PostgreSQL identifier")
        return value

    @model_validator(mode="after")
    def heartbeat_policy(self) -> Self:
        if self.heartbeat_interval >= self.heartbeat_timeout:
            raise ValueError("heartbeat_interval must be less than heartbeat_timeout")
        return self

"""Pure JSON configuration and cleanup references for the borrowed shellctl service."""

import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, NonNegativeInt, field_validator
from shellctl.shared.constants import (
    DEFAULT_AUTH_TOKEN_ENV,
    DEFAULT_BASE_URL,
    SHELL_TOOL_HARD_TIMEOUT_SECONDS,
)

OWNERSHIP_ENV = frozenset({"KAPY_SESSION_ID", "KAPY_PLUGIN_PROVIDER", "KAPY_PLUGIN_NAME"})
type NonEmpty = Annotated[str, Field(min_length=1)]
type WaitTimeout = Annotated[
    float, Field(ge=0, le=SHELL_TOOL_HARD_TIMEOUT_SECONDS, allow_inf_nan=False)
]
type RunTimeout = Annotated[
    float, Field(gt=0, le=SHELL_TOOL_HARD_TIMEOUT_SECONDS, allow_inf_nan=False)
]


class ShellPluginConfig(BaseModel):
    """Remote cwd/env are ordinary persisted config, never a secret store.

    Validation performs no I/O. token_env names an environment variable in the
    plugin process, resolved separately for every execution and session close.
    Ownership environment fields are metadata, not authorization or idempotency.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: HttpUrl = HttpUrl(DEFAULT_BASE_URL)
    cwd: str
    env: dict[str, str] = Field(default_factory=dict)
    token_env: str = DEFAULT_AUTH_TOKEN_ENV
    redact_patterns: list[str] = Field(default_factory=list)

    @field_validator("cwd")
    @classmethod
    def absolute_cwd(cls, value: str) -> str:
        if not value.startswith("/") or "\x00" in value:
            raise ValueError("cwd must be an absolute POSIX path without NUL")
        return value

    @field_validator("token_env")
    @classmethod
    def environment_name(cls, value: str) -> str:
        if not value or "=" in value or "\x00" in value:
            raise ValueError("environment names must be nonempty and contain neither '=' nor NUL")
        return value

    @field_validator("env")
    @classmethod
    def environment(cls, value: dict[str, str]) -> dict[str, str]:
        for name, content in value.items():
            cls.environment_name(name)
            if name in OWNERSHIP_ENV or "\x00" in content:
                raise ValueError("env contains reserved ownership metadata or NUL")
        return value

    @field_validator("redact_patterns")
    @classmethod
    def patterns(cls, value: list[str]) -> list[str]:
        for pattern in value:
            try:
                re.compile(pattern)
            except re.error as error:
                raise ValueError(f"Invalid redaction pattern: {error}") from error
        return value


class ShellPluginState(BaseModel):
    """Known session job IDs and byte cursors; no output or remote status cache."""

    model_config = ConfigDict(extra="forbid")

    jobs: dict[NonEmpty, NonNegativeInt] = Field(default_factory=dict)

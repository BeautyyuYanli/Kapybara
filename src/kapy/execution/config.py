"""Validated daemon startup configuration; construction performs no I/O."""

import ipaddress
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class DaemonConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    machine_id: str
    gateway_url: str
    machine_token: SecretStr
    cgroup_root: Path | None = None
    state_dir: Path | None = None
    data_dir: Path | None = None
    runtime_dir: Path | None = None
    child_env: dict[str, str] = Field(default_factory=dict, repr=False)
    idle_disconnect_after_s: float | None = Field(default=None, gt=0)
    idle_reconnect_after_s: float = Field(default=30.0, gt=0)

    @field_validator("machine_id")
    @classmethod
    def valid_machine_id(cls, value: str) -> str:
        if not value or len(value.encode("utf-8")) > 128 or any(c in value for c in "\0\r\n"):
            raise ValueError(
                "machine_id must contain 1..128 UTF-8 bytes without control separators"
            )
        return value

    @field_validator("machine_token")
    @classmethod
    def valid_machine_token(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if not secret or any(c in secret for c in "\0\r\n"):
            raise ValueError("machine_token must be nonempty and safe for an Authorization header")
        return value

    @field_validator("gateway_url")
    @classmethod
    def valid_gateway_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
        loopback = hostname == "localhost"
        if hostname is not None and not loopback:
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                pass
        if (
            not hostname
            or (parsed.scheme != "wss" and not (parsed.scheme == "ws" and loopback))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("gateway_url must be a full WSS URL, except loopback WS tests")
        return value

    @field_validator("state_dir", "data_dir", "runtime_dir", "cgroup_root")
    @classmethod
    def absolute_path(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("Daemon paths must be absolute")
        return value

    @field_validator("child_env")
    @classmethod
    def valid_child_env(cls, value: dict[str, str]) -> dict[str, str]:
        for key, content in value.items():
            if not key or "=" in key or "\0" in key + content or key.startswith("KAPY_"):
                raise ValueError("child_env has an invalid or reserved environment key/value")
        return value

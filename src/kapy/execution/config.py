"""Pure daemon configuration; construction performs no environment or filesystem I/O."""

import ipaddress
import math
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class DaemonConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    machine_id: str
    gateway_url: str
    machine_token: SecretStr
    state_dir: Path | None = None
    data_dir: Path | None = None
    runtime_dir: Path | None = None
    child_env: dict[str, str] = Field(default_factory=dict, repr=False)
    idle_disconnect_after_s: float | None = None
    idle_reconnect_after_s: float = 30.0

    @field_validator("machine_id")
    @classmethod
    def valid_machine_id(cls, value: str) -> str:
        if not value or "\0" in value:
            raise ValueError("machine_id must be nonempty and contain no NUL")
        return value

    @field_validator("machine_token")
    @classmethod
    def valid_token(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value() or any(c in value.get_secret_value() for c in "\r\n\0"):
            raise ValueError("machine_token must be nonempty and contain no control delimiters")
        return value

    @field_validator("state_dir", "data_dir", "runtime_dir")
    @classmethod
    def absolute_root(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("Execution directories must be absolute")
        return value

    @field_validator("idle_disconnect_after_s", "idle_reconnect_after_s")
    @classmethod
    def positive_seconds(cls, value: float | None) -> float | None:
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("Idle intervals must be positive and finite")
        return value

    @field_validator("gateway_url")
    @classmethod
    def valid_gateway(cls, value: str) -> str:
        parsed = urlsplit(value)
        host = parsed.hostname
        _ = parsed.port
        loopback = host == "localhost"
        if host and not loopback:
            try:
                loopback = ipaddress.ip_address(host).is_loopback
            except ValueError:
                pass
        if (
            not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
            or parsed.query
            or not parsed.path.startswith("/rpc/machines/")
            or (parsed.scheme != "wss" and not (parsed.scheme == "ws" and loopback))
        ):
            raise ValueError("gateway_url must be a complete machine WSS URL (loopback WS allowed)")
        return value

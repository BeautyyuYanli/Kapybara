"""Trusted server locations and secret-bearing grants are separate contracts.

Only server configuration can choose a URL or executable. A grant selects a
configured namespace and supplies credentials; an empty grant authorizes public
upstreams. Secret values are redacted by normal Pydantic serialization.
"""

import json
import re
from typing import Annotated, Literal, Self

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    StringConstraints,
    field_validator,
    model_validator,
)

NamespaceName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
EnvName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
INLINE_CONFIG_ENV = "MCP_GATEWAY_INLINE_CONFIG"

_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_RESERVED_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "mcp-session-id",
    "mcp-protocol-version",
    "proxy-authorization",
    "proxy-authenticate",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


def validate_headers(headers: dict[str, SecretStr]) -> dict[str, SecretStr]:
    """Normalize header names and reject transport controls and invalid values."""
    normalized: dict[str, SecretStr] = {}
    for name, secret in headers.items():
        lowered = name.lower()
        value = secret.get_secret_value()
        if (
            not _HEADER_NAME.fullmatch(name)
            or lowered in _RESERVED_HEADERS
            or lowered in normalized
            or any(ord(char) < 32 or ord(char) > 255 or ord(char) == 127 for char in value)
        ):
            raise ValueError("Invalid, duplicate or reserved upstream header")
        normalized[lowered] = secret
    return normalized


class BasicCredentials(Model):
    username: str = Field(min_length=1)
    password: SecretStr


class UpstreamCredentials(Model):
    """A per-client grant; custom data is consumed only by a trusted auth factory."""

    bearer: SecretStr | None = None
    basic: BasicCredentials | None = Field(default=None, repr=False)
    headers: dict[str, SecretStr] = Field(default_factory=dict)
    env: dict[EnvName, SecretStr] = Field(default_factory=dict)
    custom: dict[str, JsonValue] = Field(default_factory=dict, repr=False, exclude=True)

    _headers = field_validator("headers")(validate_headers)

    @model_validator(mode="after")
    def check_authorization(self) -> Self:
        if (
            sum((self.bearer is not None, self.basic is not None, "authorization" in self.headers))
            > 1
        ):
            raise ValueError("Choose one upstream Authorization mechanism")
        if self.bearer is not None:
            value = self.bearer.get_secret_value()
            if not value or any(ord(char) < 33 or ord(char) > 126 for char in value):
                raise ValueError("Invalid upstream bearer token")
        return self

    def plaintext(self) -> dict[str, JsonValue]:
        """Reveal credentials for token issuance or trusted inline startup."""
        result: dict[str, JsonValue] = {
            "headers": {name: value.get_secret_value() for name, value in self.headers.items()},
            "env": {name: value.get_secret_value() for name, value in self.env.items()},
            "custom": self.custom,
        }
        if self.bearer is not None:
            result["bearer"] = self.bearer.get_secret_value()
        if self.basic is not None:
            result["basic"] = {
                "username": self.basic.username,
                "password": self.basic.password.get_secret_value(),
            }
        return result


class HTTPUpstream(Model):
    transport: Literal["http", "sse"] = "http"
    mode: Literal["legacy", "auto"] | None = None
    url: AnyHttpUrl
    headers: dict[str, SecretStr] = Field(default_factory=dict)
    timeout: float = Field(default=30, gt=0, allow_inf_nan=False)

    _headers = field_validator("headers")(validate_headers)

    @field_validator("url")
    @classmethod
    def check_url(cls, url: AnyHttpUrl) -> AnyHttpUrl:
        if url.username is not None or url.password is not None or url.fragment is not None:
            raise ValueError("Upstream URLs cannot contain userinfo or fragments")
        return url


class StdioUpstream(Model):
    transport: Literal["stdio"] = "stdio"
    mode: Literal["legacy", "auto"] | None = None
    command: str = Field(min_length=1)
    args: list[str] = Field(default_factory=list)
    cwd: str | None = None
    env: dict[EnvName, SecretStr] = Field(default_factory=dict)
    credential_env: list[EnvName] = Field(default_factory=list)
    timeout: float = Field(default=30, gt=0, allow_inf_nan=False)


Upstream = Annotated[HTTPUpstream | StdioUpstream, Field(discriminator="transport")]


class GatewayConfig(Model):
    name: str = "MCP Gateway"
    issuer: str = Field(default="kapy-mcp-gateway", min_length=1)
    audience: str = Field(default="kapy-mcp-gateway", min_length=1)
    servers: dict[NamespaceName, Upstream] = Field(min_length=1)


def validate_credential_transport(server: Upstream, grant: UpstreamCredentials) -> None:
    """Reject credentials the selected transport cannot consume, without opening it."""
    if isinstance(server, StdioUpstream):
        if (
            grant.bearer is not None
            or grant.basic is not None
            or grant.headers
            or grant.custom
            or not grant.env.keys() <= set(server.credential_env)
        ):
            raise ValueError("Stdio credentials may only supply allowed environment names")
    elif grant.env:
        raise ValueError("HTTP credentials cannot supply environment variables")


class InlineGatewayConfig(Model):
    """A private stdio gateway's complete configuration, supplied by its parent.

    Every configured server is exposed; omitted credentials select the server's
    fixed headers/env or an anonymous connection. Validators perform no I/O.
    Ordinary serialization redacts secrets; startup_payload explicitly reveals
    them for the child process, never for downstream MCP protocol messages.
    """

    name: str = "Inline MCP Gateway"
    servers: dict[NamespaceName, Upstream] = Field(default_factory=dict)
    credentials: dict[NamespaceName, UpstreamCredentials] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def validate_credentials(self) -> Self:
        if self.credentials.keys() - self.servers.keys():
            raise ValueError("Credentials reference an unconfigured upstream")
        for name, grant in self.credentials.items():
            validate_credential_transport(self.servers[name], grant)
        return self

    def grants(self) -> dict[str, UpstreamCredentials]:
        """Authorize the configured set, including servers with no explicit credentials."""
        return {name: self.credentials.get(name, UpstreamCredentials()) for name in self.servers}

    def startup_payload(self) -> dict[str, JsonValue]:
        """Export plaintext only at the trusted process-start/persistence boundary."""
        servers: dict[str, JsonValue] = {}
        for name, server in self.servers.items():
            data = server.model_dump(mode="json")
            if isinstance(server, HTTPUpstream):
                data["headers"] = {
                    key: value.get_secret_value() for key, value in server.headers.items()
                }
            else:
                data["env"] = {key: value.get_secret_value() for key, value in server.env.items()}
            servers[name] = data
        return {
            "name": self.name,
            "servers": servers,
            "credentials": {name: grant.plaintext() for name, grant in self.credentials.items()},
        }

    def startup_json(self) -> str:
        return json.dumps(self.startup_payload(), separators=(",", ":"))

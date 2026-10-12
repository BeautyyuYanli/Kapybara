"""Serializable inline MCP configuration; connection resources never enter State.

Kapy persists Config as JSON and passes it into Temporal history. The JSON export
deliberately preserves plaintext credentials, like other execution configuration;
repr hides credentials, and no clients, processes or encryption keys are stored.
Validation and serialization perform no external I/O.
"""

from typing import Self

from mcp_gateway import InlineGatewayConfig
from mcp_gateway.gateway import validate_grants
from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


class Config(InlineGatewayConfig):
    init_timeout: float = Field(default=30, gt=0, allow_inf_nan=False)
    read_timeout: float = Field(default=300, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_launch(self) -> Self:
        # The CLI uses installed gateway code, not serialized Python auth factories.
        validate_grants(self, self.grants(), {})
        return self

    @model_serializer(mode="wrap")
    def serialize_config(
        self, handler: SerializerFunctionWrapHandler, info: SerializationInfo
    ) -> dict[str, JsonValue]:
        data = handler(self)
        if info.mode == "json":
            data.update(self.startup_payload())
        return data


class State(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GatewayEndpoint(BaseModel):
    """An ephemeral run-owned relay, carried only in execution deps, never State.

    The relay's random bearer protects its internal HTTP connection; the private
    stdio Gateway still uses no downstream authentication or JWE. Temporal stores
    this descriptor as plaintext like the other execution configuration.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    url: AnyHttpUrl
    token: str = Field(min_length=1, repr=False)
    attempt: int = Field(ge=1)

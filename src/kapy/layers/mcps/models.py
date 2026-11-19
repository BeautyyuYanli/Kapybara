"""Serializable inline MCP configuration; connection resources never enter State.

Kapy persists Config as session JSON in PostgreSQL. The JSON export includes
inline MCP credentials in the public session DTO. The model representation
hides those credentials. Run Activity input contains only session ID and prompt,
with provider credentials loaded in the Worker.
Validation and serialization perform no external I/O.
"""

from typing import Self

from mcp_gateway import InlineGatewayConfig
from mcp_gateway.gateway import validate_grants
from pydantic import (
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

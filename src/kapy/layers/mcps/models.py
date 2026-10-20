"""Serializable inline MCP configuration; connection resources never enter State.

Kapy persists Config as session JSON. Temporal retains the original create request,
including inline MCP credentials. The JSON export deliberately preserves those
plaintext credentials; repr hides them. Run Activity input contains only session
ID and prompt, with provider credentials loaded in the Worker. No clients,
processes or encryption keys are stored.
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

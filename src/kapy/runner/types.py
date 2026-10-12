"""Serializable frozen input, loaded from the operation ledger by the Worker.

The complete configuration, including the API key, is stored in private database input.
`repr=False` prevents accidental repr disclosure; it does not encrypt payloads.
Settings are final values, not overrides of a Worker-local model catalogue.
"""

from dataclasses import dataclass, field
from typing import Self
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kapy.agent_output import AgentOutputService
from kapy.control.models.runtime import provider_arguments
from kapy.control.models.types import ProviderConfig, ProviderInput
from kapy.control.types import DTO, JsonObject, Name
from pydantic_ai_layer_protocol.types import LayerData


class ExecutionConfig(DTO):
    provider_class: str
    model_class: str
    model_name: Name
    api_key: str = Field(repr=False)
    base_url: str | None = None
    provider_kwargs: JsonObject = Field(default_factory=dict, repr=False)
    model_settings: JsonObject = Field(default_factory=dict)
    context_window: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def validate_connection(self) -> Self:
        for reference in (self.provider_class, self.model_class):
            module, separator, name = reference.partition(":")
            if not separator or not module or not name or ":" in name:
                raise ValueError("Class references must use module:ClassName")
        ProviderInput.require_api_key(SecretStr(self.api_key))
        ProviderInput.validate_base_url(self.base_url)
        provider_arguments(self.provider_config())
        return self

    def provider_config(self) -> ProviderConfig:
        """Rebuild the existing process-local constructor input without serializing SecretStr."""
        return ProviderConfig(
            provider_class=self.provider_class,
            model_class=self.model_class,
            api_key=SecretStr(self.api_key),
            base_url=self.base_url,
            provider_kwargs=self.provider_kwargs,
        )


class RunnerInput(DTO):
    """Immutable input frozen in the transaction that claims the session."""

    session_id: UUID
    runner_state: str | None
    user_prompt: str
    config: ExecutionConfig
    layers: dict[str, LayerData] = Field(default_factory=dict)


@dataclass
class RunnerDeps:
    """Mutable per-run State and borrowed Worker resources, never a Temporal payload."""

    session_id: UUID
    activity_id: UUID
    session_factory: async_sessionmaker[AsyncSession]
    output_service: AgentOutputService
    layers: dict[str, LayerData] = field(default_factory=dict)
    response_seq: int | None = None

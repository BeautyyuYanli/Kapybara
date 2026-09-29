"""Serializable execution input, prepared by callers rather than resolved from a session.

The complete configuration, including the API key, is stored in Temporal history.
`repr=False` prevents accidental repr disclosure; it does not encrypt payloads.
Settings are final values, not overrides of a Worker-local model catalogue.
"""

from typing import Self
from uuid import UUID

from pydantic import Field, SecretStr, model_validator

from kapy.agent_runner.types import HistoryMessage
from kapy.control.models.runtime import provider_arguments
from kapy.control.models.types import ProviderConfig, ProviderInput
from kapy.control.types import DTO, JsonObject, Name


class DurableExecutionConfig(DTO):
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
    """The caller reads state and version together and serializes runs per session."""

    session_id: UUID
    runner_state_version: int = Field(ge=0, strict=True)
    runner_state: str | None
    user_prompt: str
    config: DurableExecutionConfig


class RunnerDeps(DTO):
    """Run input plus the current request's provisional response position."""

    config: DurableExecutionConfig
    session_id: UUID
    response_seq: int | None = Field(default=None, ge=0, strict=True)


class MessageBatch(DTO):
    """One atomic batch with explicit positions, authority and complete SDK messages."""

    session_id: UUID
    messages: list[HistoryMessage]


class SaveRunnerStateInput(DTO):
    """Every Activity retry retains this exact version and encoded state."""

    session_id: UUID
    expected_version: int = Field(ge=0, strict=True)
    runner_state: str

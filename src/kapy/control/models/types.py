"""Provider credentials are accepted on writes and exposed only to internal runtime code."""

from datetime import datetime
from uuid import UUID

from pydantic import Field, HttpUrl, SecretStr, field_validator

from kapy.control.types import DTO, JsonObject, Name, UpdateDTO


class ProviderInput(DTO):
    @field_validator("api_key", check_fields=False)
    @classmethod
    def require_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().strip():
            raise ValueError("api_key cannot be empty")
        return value

    @field_validator("base_url", check_fields=False)
    @classmethod
    def validate_base_url(cls, value: str | None) -> str | None:
        """Validate HTTP syntax without rewriting the configured SDK base."""
        if value is not None:
            url = HttpUrl(value)
            if (
                url.username is not None
                or url.password is not None
                or url.query is not None
                or url.fragment is not None
            ):
                raise ValueError(
                    "base_url must be an HTTP(S) URL without credentials, query or fragment"
                )
        return value


class CreateProvider(ProviderInput):
    name: Name
    provider_class: str
    model_class: str
    api_key: SecretStr
    base_url: str | None = None
    provider_kwargs: JsonObject = Field(default_factory=dict, repr=False)


class UpdateProvider(UpdateDTO, ProviderInput):
    nullable_fields = frozenset({"base_url"})
    name: Name | None = None
    base_url: str | None = None
    api_key: SecretStr | None = None
    provider_kwargs: JsonObject | None = Field(default=None, repr=False)


class CreateModel(DTO):
    provider_id: UUID
    model_name: Name
    name: Name | None = None
    settings: JsonObject = Field(default_factory=dict)
    context_window: int | None = Field(default=None, gt=0, strict=True)


class UpdateModel(UpdateDTO):
    nullable_fields = frozenset({"context_window"})
    name: Name | None = None
    settings: JsonObject | None = None
    context_window: int | None = Field(default=None, gt=0, strict=True)


class ProviderRecord(DTO):
    """Readable constructor configuration; only api_key remains write-only."""

    id: UUID
    name: str
    provider_class: str
    model_class: str
    base_url: str | None
    provider_kwargs: JsonObject
    created_at: datetime
    updated_at: datetime


class ModelRecord(DTO):
    provider_id: UUID
    model_name: str
    name: str
    settings: JsonObject
    context_window: int | None
    created_at: datetime
    updated_at: datetime


class ProviderConfig(DTO):
    """Internal connection value; never returned by provider CRUD."""

    provider_class: str
    model_class: str
    api_key: SecretStr
    base_url: str | None
    provider_kwargs: JsonObject = Field(repr=False)


class ModelAlreadyExists(ValueError):
    pass


class ModelDiscoveryError(RuntimeError):
    pass

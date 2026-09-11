"""Shared control DTO conventions; stored configuration contains only JSON values."""

from datetime import UTC, datetime
from typing import Annotated, ClassVar, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

type JsonObject = dict[str, JsonValue]
type Name = Annotated[str, Field(min_length=1, max_length=256)]


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class UpdateDTO(DTO):
    """Omission preserves a field; only explicitly nullable columns accept None."""

    nullable_fields: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def reject_nulls(self) -> Self:
        for name in self.model_fields_set - self.nullable_fields:
            if getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


def utc_now() -> datetime:
    return datetime.now(UTC)

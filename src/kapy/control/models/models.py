"""Local provider/model configuration, with application-checked references and no FKs."""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import JSON, CheckConstraint, Column, DateTime, Text
from sqlmodel import Field

from kapy.control.database import ControlTable
from kapy.control.types import utc_now


class ProviderRow(ControlTable, table=True):
    __tablename__ = "providers"  # pyrefly: ignore[bad-override]

    id: UUID = Field(default_factory=uuid4, primary_key=True)
    name: str = Field(sa_type=Text)
    provider_class: str = Field(sa_type=Text)
    model_class: str = Field(sa_type=Text)
    api_key: str = Field(sa_type=Text, repr=False)
    base_url: str | None = Field(default=None, sa_type=Text)
    provider_kwargs: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON(none_as_null=True), nullable=False), repr=False
    )
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )


class ModelRow(ControlTable, table=True):
    __tablename__ = "models"  # pyrefly: ignore[bad-override]
    __table_args__ = (CheckConstraint("context_window > 0"),)

    provider_id: UUID = Field(primary_key=True)
    model_name: str = Field(primary_key=True, sa_type=Text)
    name: str = Field(sa_type=Text)
    settings: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON(none_as_null=True), nullable=False)
    )
    context_window: int | None = None
    created_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )
    updated_at: datetime = Field(
        default_factory=utc_now, sa_column=Column(DateTime(timezone=True), nullable=False)
    )

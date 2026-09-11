"""Telegram's private SQLite tables; business UUIDs are values, never foreign keys."""

from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import JSON, Column, MetaData, Text
from sqlmodel import Field, SQLModel


class TelegramTable(SQLModel):
    metadata: ClassVar[MetaData] = MetaData()


class PollRow(TelegramTable, table=True):
    __tablename__ = "plugin_telegram_poll"  # pyrefly: ignore[bad-override]
    bot_id: int = Field(primary_key=True)
    next_update_id: int = 0


class DefaultModelRow(TelegramTable, table=True):
    """One bot-wide model selection for future sessions, independent of chat routes."""

    __tablename__ = "plugin_telegram_defaults"  # pyrefly: ignore[bad-override]
    bot_id: int = Field(primary_key=True)
    provider_id: UUID
    model_name: str = Field(sa_type=Text)


class InboxRow(TelegramTable, table=True):
    __tablename__ = "plugin_telegram_inbox"  # pyrefly: ignore[bad-override]
    bot_id: int = Field(primary_key=True)
    update_id: int = Field(primary_key=True)
    chat_id: int
    thread_id: int = 0
    payload: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    resolved_action: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    handled: bool = False
    next_attempt_at: float = 0


class RouteRow(TelegramTable, table=True):
    __tablename__ = "plugin_telegram_routes"  # pyrefly: ignore[bad-override]
    bot_id: int = Field(primary_key=True)
    chat_id: int = Field(primary_key=True)
    thread_id: int = Field(primary_key=True)
    session_id: UUID


class DeliveryRow(TelegramTable, table=True):
    __tablename__ = "plugin_telegram_delivery"  # pyrefly: ignore[bad-override]
    bot_id: int = Field(primary_key=True)
    chat_id: int = Field(primary_key=True)
    thread_id: int = Field(primary_key=True)
    session_id: UUID = Field(primary_key=True)
    chat_type: str = Field(sa_type=Text)
    after_seq: int = -1
    pending: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    item_offset: int = 0
    next_attempt_at: float = 0
    blocked_error: str | None = Field(default=None, sa_type=Text)

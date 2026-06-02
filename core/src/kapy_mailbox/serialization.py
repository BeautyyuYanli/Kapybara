"""Serialization helpers for mailbox messages.

The default serializer uses Pydantic v2 as the storage boundary validator.
Payloads must validate as `JsonValue`, and stored mailbox records are encoded as
Pydantic models so PostgreSQL text/json columns stay JSON-inspectable without
hand-written schema parsing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from kapy_mailbox.exceptions import PayloadSerializationError
from kapy_mailbox.models import JsonValue, Message


def _normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


class _StoredMessageModel(BaseModel):
    """Serialized mailbox message record stored in mailbox persistence."""

    model_config = ConfigDict(extra="forbid")

    id: str
    channel: str
    payload: JsonValue
    created_at: datetime
    producer: str | None = None


class _ConsumedInfoModel(BaseModel):
    """Serialized consume-state side record stored in mailbox persistence."""

    model_config = ConfigDict(extra="forbid")

    consumed_at: datetime
    consumed_by: str | None = None


_JSON_VALUE_ADAPTER = TypeAdapter(JsonValue)


class MessageSerializer(Protocol):
    """Encodes payloads and stored message records for mailbox persistence."""

    def normalize_payload(self, payload: JsonValue) -> JsonValue:
        """Validate and deep-copy a payload into a JSON-compatible value."""

        ...

    def dump_message(self, message: Message) -> str:
        """Serialize a message record for durable mailbox storage."""

        ...

    def load_message(
        self,
        raw_message: str | bytes,
        *,
        raw_consumed_info: str | bytes | None = None,
    ) -> Message:
        """Deserialize a message record and optional consumed-info overlay."""

        ...

    def dump_consumed_info(
        self, consumed_at: datetime, *, consumed_by: str | None
    ) -> str:
        """Serialize the consumed-info side record."""

        ...


@dataclass(slots=True, frozen=True)
class JSONMessageSerializer:
    """Default mailbox serializer backed by Pydantic validation and JSON export."""

    def normalize_payload(self, payload: JsonValue) -> JsonValue:
        try:
            normalized = _JSON_VALUE_ADAPTER.validate_python(payload)
            # Round-trip through Pydantic JSON output so callers receive a clean
            # JSON-compatible deep copy rather than references into their input.
            return cast(
                JsonValue,
                _JSON_VALUE_ADAPTER.validate_json(
                    _JSON_VALUE_ADAPTER.dump_json(normalized)
                ),
            )
        except ValidationError as exc:
            raise PayloadSerializationError(str(exc)) from exc

    def dump_message(self, message: Message) -> str:
        record = _StoredMessageModel(
            id=message.id,
            channel=message.channel,
            payload=message.payload,
            created_at=_normalize_datetime(message.created_at),
            producer=message.producer,
        )
        return record.model_dump_json()

    def load_message(
        self,
        raw_message: str | bytes,
        *,
        raw_consumed_info: str | bytes | None = None,
    ) -> Message:
        try:
            message_data = _StoredMessageModel.model_validate_json(
                _decode_text(raw_message)
            )
        except ValidationError as exc:
            raise PayloadSerializationError(str(exc)) from exc

        consumed_at: datetime | None = None
        consumed_by: str | None = None
        if raw_consumed_info is not None:
            try:
                consumed_info_data = _ConsumedInfoModel.model_validate_json(
                    _decode_text(raw_consumed_info)
                )
            except ValidationError as exc:
                raise PayloadSerializationError(str(exc)) from exc
            consumed_at = _normalize_datetime(consumed_info_data.consumed_at)
            consumed_by = consumed_info_data.consumed_by

        return Message(
            id=message_data.id,
            channel=message_data.channel,
            payload=cast(JsonValue, message_data.payload),
            created_at=_normalize_datetime(message_data.created_at),
            producer=message_data.producer,
            consumed_at=consumed_at,
            consumed_by=consumed_by,
        )

    def dump_consumed_info(
        self, consumed_at: datetime, *, consumed_by: str | None
    ) -> str:
        record = _ConsumedInfoModel(
            consumed_at=_normalize_datetime(consumed_at),
            consumed_by=consumed_by,
        )
        return record.model_dump_json()


def _decode_text(value: str | bytes) -> str:
    if isinstance(value, bytes):
        return value.decode()
    return value

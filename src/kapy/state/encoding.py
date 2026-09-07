"""Bounded wire encoding and session-bound record positions."""

import base64
import hashlib
import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from .contracts import InvalidArgument

PAGE_BYTES = 512 * 1024
ITEM_BYTES = 256 * 1024
DELTA_BYTES = 16 * 1024
CHECKPOINT_BYTES = 4 * 1024 * 1024


def _default(value: object) -> object:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    raise TypeError("value is not JSON serializable")


def encode(value: object) -> bytes:
    # Default separators/ASCII escaping are intentionally conservative for RPC codecs.
    try:
        return json.dumps(value, default=_default, ensure_ascii=True, allow_nan=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidArgument("value must be finite JSON") from exc


def plain(value: object) -> Any:
    return json.loads(encode(value))


def bounded(value: object, maximum: int = ITEM_BYTES) -> None:
    encoded = encode(value)
    if len(encoded) > maximum:
        raise InvalidArgument(f"encoded item exceeds {maximum} bytes")
    # PostgreSQL JSONB/text cannot store NUL or unpaired Unicode surrogates.
    pending = [json.loads(encoded)]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            try:
                item.encode("utf-8")
            except UnicodeError as exc:
                raise InvalidArgument("JSON contains an invalid Unicode scalar") from exc
            if "\0" in item:
                raise InvalidArgument("PostgreSQL JSON strings cannot contain NUL")


def fingerprint(value: object) -> str:
    canonical = json.dumps(plain(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def cursor(session_id: UUID, seq: int) -> str:
    return base64.urlsafe_b64encode(f"1:{session_id}:{seq}".encode()).decode().rstrip("=")


def sequence(session_id: UUID, value: str | None) -> int:
    if value is None:
        return 0
    try:
        if len(value) > 128:
            raise ValueError
        version, owner, raw = (
            base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
            .decode()
            .split(":")
        )
        seq = int(raw)
        if version != "1" or UUID(owner) != session_id or seq < 0 or seq > 2**63 - 1:
            raise ValueError
        return seq
    except (ValueError, UnicodeError) as exc:
        raise InvalidArgument("invalid cursor for this session") from exc


def page_limit(limit: int) -> None:
    if isinstance(limit, bool) or not 1 <= limit <= 200:
        raise InvalidArgument("limit must be between 1 and 200")

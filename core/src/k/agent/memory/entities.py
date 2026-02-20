"""Pydantic entities for agent memory records.

ID scheme
---------
`MemoryRecord.id_` is a string identifier.

- New records default to a fixed-length, order-preserving encoding of the
  millisecond POSIX timestamp of `created_at`:
  - Compute integer POSIX milliseconds for `created_at`.
  - Encode as a big-endian, unsigned 48-bit integer (6 bytes).
  - Format as an 8-character, URL-safe base64-like string using a custom
    alphabet whose ASCII order matches digit values (so lexicographic order
    matches time order).

  This keeps the underlying payload a fixed 6 bytes and ensures lexicographic
  order over the string ids matches chronological order by `created_at`.
  This is intentionally *not* RFC 4648 base64url: the alphabet is chosen to make
  lexicographic order match numeric order.

Note: because the id is derived from millisecond resolution, two records created
in the same millisecond would collide; stores should continue to reject duplicate
ids on append.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_ai.messages import ModelRequest, ModelResponse

from k.agent.channels import (
    effective_out_channel,
    normalize_out_channel,
    validate_channel_path,
)

_ORDERED_B64_MILLIS_8_RE = re.compile(r"^[-0-9A-Z_a-z]{8}$")
_ORDERED_B64_ALPHABET = (
    "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"
)
_ORDERED_B64_DECODE = {ch: idx for idx, ch in enumerate(_ORDERED_B64_ALPHABET)}

_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)


def _datetime_to_posix_millis(value: datetime) -> int:
    """Return the integer POSIX milliseconds represented by `value`.

    For timezone-aware datetimes, prefer integer delta arithmetic over
    `datetime.timestamp()` to avoid float rounding affecting the millisecond
    value (and therefore id ordering).

    For naive datetimes, defer to `datetime.timestamp()` which interprets the
    value in the system local timezone.
    """

    if value.tzinfo is None:
        return int(value.timestamp() * 1000)

    delta = value.astimezone(UTC) - _EPOCH_UTC
    return delta.days * 86_400_000 + delta.seconds * 1000 + (delta.microseconds // 1000)


def memory_record_id_from_millis(millis: int) -> str:
    """Return an id for the given integer POSIX millisecond timestamp."""

    if millis < 0 or millis >= 1 << 48:
        raise ValueError(f"created_at millis out of range for 48-bit id: {millis}")

    chars: list[str] = []
    for shift in range(42, -1, -6):
        chars.append(_ORDERED_B64_ALPHABET[(millis >> shift) & 0x3F])
    return "".join(chars)


def memory_record_id_from_created_at(created_at: datetime) -> str:
    """Return the id derived from `created_at`'s millisecond POSIX timestamp.

    The returned id is a fixed-length, lexicographically sortable
    encoding of the big-endian 48-bit millisecond timestamp.
    """

    return memory_record_id_from_millis(_datetime_to_posix_millis(created_at))


def is_memory_record_id(value: str) -> bool:
    """Return true if `value` is a valid MemoryRecord id string."""

    if not _ORDERED_B64_MILLIS_8_RE.fullmatch(value):
        return False
    decoded = 0
    for ch in value:
        digit = _ORDERED_B64_DECODE.get(ch)
        if digit is None:
            return False
        decoded = (decoded << 6) | digit
    return 0 <= decoded < (1 << 48)


class MemoryRecord(BaseModel):
    """Persisted memory record with hierarchical channel routing metadata.

    Notes:
    - `in_channel` is required for all records.
    - `out_channel=None` means "same destination as input channel".
    """

    created_at: datetime = Field(default_factory=datetime.now)
    in_channel: str
    out_channel: str | None = None
    id_: str = ""
    parents: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)

    input: str
    compacted: list[str] = Field(default_factory=list)
    output: str
    detailed: list[ModelRequest | ModelResponse] = Field(default_factory=list)

    @field_validator("in_channel")
    @classmethod
    def _validate_in_channel(cls, value: str) -> str:
        return validate_channel_path(value, field_name="in_channel")

    @field_validator("out_channel")
    @classmethod
    def _validate_out_channel(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return validate_channel_path(value, field_name="out_channel")

    @model_validator(mode="after")
    def _finalize_and_validate_ids(self) -> MemoryRecord:
        self.out_channel = normalize_out_channel(
            in_channel=self.in_channel,
            out_channel=self.out_channel,
        )

        if not self.id_:
            self.id_ = memory_record_id_from_created_at(self.created_at)
        if not is_memory_record_id(self.id_):
            raise ValueError(f"Invalid MemoryRecord id: {self.id_!r}")

        for link_name, ids in (("parents", self.parents), ("children", self.children)):
            bad = [i for i in ids if not is_memory_record_id(i)]
            if bad:
                raise ValueError(f"Invalid MemoryRecord {link_name} id(s): {bad!r}")
        return self

    @property
    def effective_out_channel(self) -> str:
        return effective_out_channel(
            in_channel=self.in_channel,
            out_channel=self.out_channel,
        )

    @property
    def short_id(self) -> str:
        return self.id_[:8]

    def dump_raw_pair(self) -> str:
        # return self.model_dump_json(exclude={"detailed", "compacted"})
        return f"""<Meta>{self.model_dump_json(include={"id_", "parents", "children"})}</Meta><Instruct>{self.input}</Instruct><Response>{self.output}</Response>"""

    def dump_compated(self) -> str:
        # return self.model_dump_json(exclude={"detailed"})
        return f"""<Meta>{self.model_dump_json(include={"id_", "parents", "children"})}</Meta><Instruct>{self.input}</Instruct><Process>{self.compacted}</Process><Response>{self.output}</Response>"""

"""Pydantic entities for agent memory records.

ID scheme
---------
`MemoryRecord.id_` is a string identifier.

- New records default to a base64url-encoded, big-endian 48-bit integer derived
  from the millisecond POSIX timestamp of `created_at` (no padding). This yields
  an 8-character id (e.g., ``AAAAAAAA`` for epoch millis 0).
- Legacy UUID strings are still accepted to keep older persisted records readable.

Note: because the id is derived from millisecond resolution, two records created
in the same millisecond would collide; stores should continue to reject duplicate
ids on append.
"""

from __future__ import annotations

import base64
import binascii
import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator
from pydantic_ai.messages import ModelRequest, ModelResponse

_B64URL_8_RE = re.compile(r"^[A-Za-z0-9_-]{8}$")


def memory_record_id_from_created_at(created_at: datetime) -> str:
    """Return the base64url id derived from `created_at`'s millisecond timestamp."""

    millis = int(created_at.timestamp() * 1000)
    if millis < 0 or millis >= 1 << 48:
        raise ValueError(f"created_at millis out of range for 48-bit id: {millis}")
    raw = millis.to_bytes(6, byteorder="big", signed=False)
    return base64.urlsafe_b64encode(raw).decode("ascii")


def is_memory_record_id(value: str) -> bool:
    """Return true if `value` is a base64url millis id or a UUID string."""

    try:
        UUID(value)
        return True
    except ValueError:
        pass

    if not _B64URL_8_RE.fullmatch(value):
        return False
    try:
        decoded = base64.urlsafe_b64decode(value)
    except ValueError, binascii.Error, TypeError:
        return False
    return len(decoded) == 6


class MemoryRecord(BaseModel):
    created_at: datetime = Field(default_factory=datetime.now)
    id_: str = ""
    parents: list[str] = Field(default_factory=list)
    children: list[str] = Field(default_factory=list)

    raw_pair: tuple[str, str]
    compacted: list[str]
    detailed: list[ModelRequest | ModelResponse] = Field(default_factory=list)

    @model_validator(mode="after")
    def _finalize_and_validate_ids(self) -> MemoryRecord:
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
    def short_id(self) -> str:
        return self.id_[:8]

    def dump_raw_pair(self) -> str:
        # return self.model_dump_json(exclude={"detailed", "compacted"})
        return f"""<Meta>{self.model_dump_json(include={"id_", "parents", "children"})}</Meta><Instruct>{self.raw_pair[0]}</Instruct><Response>{self.raw_pair[1]}</Response>"""

    def dump_compated(self) -> str:
        # return self.model_dump_json(exclude={"detailed"})
        return f"""<Meta>{self.model_dump_json(include={"id_", "parents", "children"})}</Meta><Instruct>{self.raw_pair[0]}</Instruct><Process>{self.compacted}</Process><Response>{self.raw_pair[1]}</Response>"""

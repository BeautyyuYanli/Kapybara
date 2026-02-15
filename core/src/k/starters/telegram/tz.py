"""Timezone parsing/rendering helpers for the Telegram starter.

Telegram's `date` fields are unix seconds (UTC). The starter renders these as
ISO-8601 datetime strings in a configurable timezone, while preserving the
original seconds as `date_unix` for exact comparisons.
"""

from __future__ import annotations

import datetime
import re
from typing import Final
from zoneinfo import ZoneInfo

_DEFAULT_TIMEZONE: Final[str] = "UTC+8"
_DEFAULT_TZINFO: Final[datetime.tzinfo] = datetime.timezone(datetime.timedelta(hours=8))


def _parse_timezone(value: str) -> datetime.tzinfo:
    """Parse a timezone argument into a tzinfo.

    Accepted forms:
    - IANA timezone name, e.g. "Asia/Shanghai"
    - UTC offsets: "UTC+8", "UTC+08:00", "+08:00", "-05", "-0530"
    - "UTC" / "Z"
    """

    raw = value.strip()
    if not raw:
        raise ValueError("timezone must be a non-empty string")

    upper = raw.upper()
    if upper in {"UTC", "Z"}:
        return datetime.UTC

    m = re.fullmatch(
        r"(?:(?:UTC)?\s*)?([+-])\s*(\d{1,2})(?::?(\d{2}))?\s*",
        raw,
        flags=re.IGNORECASE,
    )
    if m is not None:
        sign_s, hours_s, minutes_s = m.groups()
        hours = int(hours_s)
        minutes = int(minutes_s) if minutes_s is not None else 0
        if hours > 23:
            raise ValueError(f"Invalid timezone offset hours: {hours}")
        if minutes > 59:
            raise ValueError(f"Invalid timezone offset minutes: {minutes}")
        sign = 1 if sign_s == "+" else -1
        delta = datetime.timedelta(hours=hours * sign, minutes=minutes * sign)
        return datetime.timezone(delta)

    try:
        return ZoneInfo(raw)
    except Exception as e:  # pragma: no cover (platform tzdata dependent)
        raise ValueError(
            f"Invalid timezone {value!r}. Use an IANA name (e.g. 'Asia/Shanghai') "
            "or an offset (e.g. 'UTC+8', '+08:00')."
        ) from e


def _format_unix_seconds(unix_seconds: int, *, tz: datetime.tzinfo) -> str:
    """Render unix seconds as an ISO-8601 datetime string in `tz`."""

    dt = datetime.datetime.fromtimestamp(unix_seconds, tz=tz)
    return dt.isoformat(timespec="seconds")

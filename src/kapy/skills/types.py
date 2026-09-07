"""Public skill catalog values and safe business errors."""

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class SkillInfo:
    id: str
    name: str
    description: str
    revision: int
    sha256: str
    archive_bytes: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SkillDescription:
    id: str
    description: str


@dataclass(frozen=True, slots=True)
class SkillDetail:
    info: SkillInfo
    skill_md: str


class InvalidSkill(Exception):
    """The archive or its metadata does not follow the skill format."""


class SkillNotFound(Exception):
    """The skill no longer exists."""


class SkillConflict(Exception):
    """A name, revision, or request key conflicts with existing data."""


class SkillTooLarge(Exception):
    """A skill exceeds an explicit storage or extraction limit."""

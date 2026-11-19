"""Local session lifecycle with permanent tombstones for closed identities."""

from enum import StrEnum


class LifecycleStatus(StrEnum):
    READY = "ready"
    CLOSED = "closed"


class LifecycleError(RuntimeError):
    """A closed session cannot be changed or executed."""
